from dataclasses import replace
import json

import pytest

from tools.dfm.analyzers.occt import _geometric_record, _result_binding_key
from tools.dfm.contracts import (
    EffectiveRule,
    GeometricRuleBinding,
    GeometricRuleOperand,
    GeometricRequest,
    PlanFragmentRecord,
    PlanRecord,
)
from tools.dfm.errors import DFMError
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.geometry.result_identity import ResultCoverage
from tools.dfm.runtime.geometric_batches import request_key
from tools.dfm.runtime.result_documents import (
    read_result_document,
    write_result_document,
)


def model(coverage=None):
    binding = GeometricRuleBinding(
        "transition",
        "G_WALL_TRANS_DT",
        "rule",
        ">=",
        "identity",
        feature_refs=["wall"],
        check_id="C_WALL_TRANSITION",
        operand_alias="delta",
        additional_operands=[
            GeometricRuleOperand("length", "G_WALL_TRANS_LEN", feature_refs=["wall"])
        ],
        expression={
            "op": "divide",
            "args": [{"operand": "length"}, {"operand": "delta"}],
        },
    )
    fragment = PlanFragmentRecord(
        "fragment",
        "snapshot",
        "discovery",
        "2.5.0:test",
        "a" * 64,
        [],
        ["transition"],
        [GeometricRequest("wall", ["G_WALL_TRANS_DT", "G_WALL_TRANS_LEN"], {})],
    )
    plan = PlanRecord(
        "plan",
        "step",
        ["occt_cpp"],
        "ready",
        "now",
        process="injection",
        rules={"rule": EffectiveRule(3.0, "ratio", "fixture")},
        rule_bindings=[binding],
        plan_fragments=[fragment],
    )
    results = []
    for instance, delta, length in [("first", 1.0, 2.0), ("second", 2.0, 8.0)]:
        for gid, value in [("G_WALL_TRANS_DT", delta), ("G_WALL_TRANS_LEN", length)]:
            results.append(
                _geometric_record(
                    {
                        "feature_id": "wall",
                        "geometric_id": gid,
                        "status": "ok",
                        "value": value,
                        "unit": "mm",
                        "context": {},
                        "evidence": {"geometry_refs": [], "field_refs": []},
                        "quality": {
                            "measurement_instance_id": instance,
                            **({"measurement_coverage": {"status": coverage, "face_coverage_ratio": 0.01}} if coverage else {}),
                        },
                        "provenance": {
                            "dependency_fingerprint": "b" * 64,
                            "source_result_ref": gid + ":" + instance,
                        },
                    },
                    fragment,
                    "snapshot",
                    "c" * 64,
                )
            )
    return plan, results


@pytest.mark.parametrize("coverage", [None, "partial", "failed", "unknown"])
def test_instance_id_survives_result_index_and_persistence_without_overwrite(tmp_path, coverage):
    plan, results = model(coverage)
    assert len({r.result_id for r in results}) == 4
    assert len({_result_binding_key(r): r for r in results}) == 4
    path = tmp_path / "geometric_results.json"
    write_result_document(
        path, "geometric_results", {"results": [r.to_dict() for r in results]}
    )
    restored = read_result_document(path, "geometric_results")["results"]
    assert {r["result_id"] for r in restored} == {r.result_id for r in results}
    evaluations, sources = EvaluationEngine().evaluate(list(reversed(results)), plan)
    by_instance = {
        sources[e.evaluation_id]["measurement_instance_id"]: e for e in evaluations
    }
    assert by_instance["first"].actual == 2.0
    assert by_instance["first"].outcome == "fail"
    assert by_instance["second"].actual == 4.0
    assert by_instance["second"].outcome == "pass"
    assert len({e.evaluation_id for e in evaluations}) == 2
    for instance, evaluation in by_instance.items():
        assert set(evaluation.measurement_ids) == {
            r.result_id for r in results if r.measurement_instance_id == instance
        }


def test_missing_paired_operand_is_indeterminate_instead_of_using_another_instance():
    plan, results = model()
    results = [
        r
        for r in results
        if not (
            r.geometric_id == "G_WALL_TRANS_LEN"
            and r.measurement_instance_id == "first"
        )
    ]
    evaluations, sources = EvaluationEngine().evaluate(results, plan)
    by_instance = {
        sources[e.evaluation_id]["measurement_instance_id"]: e for e in evaluations
    }
    assert by_instance["first"].outcome == "indeterminate"
    assert by_instance["second"].outcome == "pass"


def test_explicit_reduction_keeps_the_full_series():
    plan, results = model()
    binding = replace(
        plan.rule_bindings[0],
        aggregation="maximum",
        additional_operands=[],
        expression=None,
    )
    plan = replace(
        plan,
        rule_bindings=[binding],
        rules={"rule": EffectiveRule(1.5, "mm", "fixture")},
    )
    evaluations, _ = EvaluationEngine().evaluate(results, plan)
    assert len(evaluations) == 1
    assert evaluations[0].actual == 2.0


@pytest.mark.parametrize("coverage", [None, "partial", "failed", "unknown"])
def test_measurement_rule_selection_happens_per_physical_instance(coverage):
    plan, results = model(coverage)
    base = plan.rule_bindings[0]

    def candidate(name, operator, value, default, threshold):
        return replace(
            base,
            binding_id=name,
            rule_id=name,
            operand_alias="G_WALL_TRANS_DT",
            expression={"operand": "G_WALL_TRANS_DT"},
            additional_operands=[],
            rule_selection={
                "conditions": [
                    {
                        "geometric_id": "G_WALL_TRANS_DT",
                        "operator": operator,
                        "value": value,
                        "unit": "mm",
                    }
                ],
                "priority": 10,
                "specificity": 1,
                "is_default": default,
            },
        )

    bindings = [
        candidate("small", "LTE", 1.5, False, 0.5),
        candidate("large", "GT", 1.5, False, 2.5),
    ]
    plan = replace(
        plan,
        rule_bindings=bindings,
        rules={
            name: EffectiveRule(value, "mm", "fixture")
            for name, value in [("small", 0.5), ("large", 2.5)]
        },
        plan_fragments=[
            replace(plan.plan_fragments[0], rule_binding_refs=["small", "large"])
        ],
    )
    evaluations, sources = EvaluationEngine().evaluate(results, plan)
    assert {
        (e.rule_id, sources[e.evaluation_id]["measurement_instance_id"])
        for e in evaluations
    } == {("small", "first"), ("large", "second")}


def test_result_coverage_accepts_named_instances_but_requires_every_request():
    key = request_key("wall", "G_WALL_TRANS_DT", {})
    second = request_key("wall", "G_WALL_TRANS_LEN", {})
    coverage = ResultCoverage({key, second})
    coverage.accept(key, {"measurement_instance_id": "first"})
    coverage.accept(key, {"measurement_instance_id": "second"})
    with pytest.raises(DFMError, match="omitted"):
        coverage.require_complete()
    coverage.accept(second, None)
    coverage.require_complete()


@pytest.mark.parametrize(
    "first,second", [(None, None), ("same", "same"), (None, "named"), ("named", None)]
)
def test_result_coverage_rejects_ambiguous_or_duplicate_instances(first, second):
    key = request_key("wall", "G_WALL_TRANS_DT", {})
    coverage = ResultCoverage({key})
    quality = lambda value: (
        None if value is None else {"measurement_instance_id": value}
    )
    coverage.accept(key, quality(first))
    with pytest.raises(DFMError, match="ambiguous"):
        coverage.accept(key, quality(second))


def test_result_coverage_rejects_unsolicited_quantities():
    coverage = ResultCoverage(set())
    with pytest.raises(DFMError, match="unrequested"):
        coverage.accept(request_key("wall", "G_WALL_TRANS_DT", {}), None)


@pytest.mark.parametrize("coverage", [None, "partial", "unknown"])
def test_transition_acceptance_criteria_evaluate_small_sampled_regions(coverage):
    plan, template = model(coverage)
    binding = replace(plan.rule_bindings[0], operator=">", expression={"operand": "delta"}, acceptance_criteria_json=[[
        {"criterion_id": "positive_delta", "comparator": "GT",
         "expression": {"operand": "delta"}, "result_unit": "mm", "threshold": 0},
        {"criterion_id": "smooth_transition", "comparator": "GTE",
         "expression": {"op": "divide", "args": [{"operand": "length"}, {"operand": "delta"}]},
         "result_unit": "ratio", "threshold": 3},
    ]])
    plan = replace(plan, rule_bindings=[binding], rules={"rule": EffectiveRule(0, "mm", "fixture")})
    # Four measured transitions from 8-10.stp, plus a passing transition.
    pairs = [(0.2501508100999008, 0.6531462962911675),
             (0.19895528582998945, 0.5508365876646543),
             (0.22540212723490827, 0.6192459303670997),
             (0.267882942043723, 0.6650858114460845), (1.0, 4.0)]
    results = [replace(row, result_id=f"r-{i}-{row.geometric_id}", value=value,
                       quality={**row.quality, "measurement_instance_id": f"instance-{i}"})
               for i, pair in enumerate(pairs)
               for row, value in zip(template[:2], pair)]
    evaluations, sources = EvaluationEngine().evaluate(results, plan)
    by_instance = {sources[e.evaluation_id]["measurement_instance_id"]: e for e in evaluations}
    assert len(evaluations) == 5
    for i, (delta, length) in enumerate(pairs):
        evaluation = by_instance[f"instance-{i}"]
        assert evaluation.outcome == ("fail" if i < 4 else "pass")
        assert evaluation.criterion_results[0]["outcome"] == "pass"
        assert evaluation.criterion_results[1]["actual"] == pytest.approx(length / delta)
        assert set(evaluation.measurement_ids) == {r.result_id for r in results
            if r.measurement_instance_id == f"instance-{i}"}


def gated_model():
    plan, results = model()
    criterion = {
        "criterion_id": "smooth_transition", "comparator": "GTE",
        "expression": plan.rule_bindings[0].expression,
        "result_unit": "ratio", "threshold": 3,
    }
    binding = replace(plan.rule_bindings[0], acceptance_criteria_json=[[criterion]],
        evaluation_gate_json={"expression": {"operand": "delta"}, "comparator": "GT",
                              "threshold": 0.2, "result_unit": "mm"})
    return replace(plan, rule_bindings=[binding]), results


@pytest.mark.parametrize("delta", [0, 0.1, 0.2])
def test_false_gate_skips_acceptance_even_when_required_length_is_absent(delta):
    plan, rows = gated_model()
    rows = [replace(row, value=delta) for row in rows
            if row.geometric_id == "G_WALL_TRANS_DT"]
    evaluations, sources = EvaluationEngine().evaluate(rows, plan)
    assert len(evaluations) == 2
    for evaluation in evaluations:
        assert evaluation.outcome == "not_applicable"
        assert evaluation.criterion_results == []
        assert evaluation.evaluation_gate_result["outcome"] == "false"
        assert sources[evaluation.evaluation_id]["acceptance_skipped"] is True
        assert evaluation.evaluation_gate_result["actual"] == delta


def test_true_gate_keeps_existing_acceptance_and_exact_greater_than_boundary():
    plan, rows = gated_model()
    rows = [replace(row, value=0.20000000000001 if row.geometric_id == "G_WALL_TRANS_DT" else 0.7)
            for row in rows]
    evaluations, _ = EvaluationEngine().evaluate(rows, plan)
    assert len(evaluations) == 2
    assert all(row.outcome == "pass" for row in evaluations)
    assert all(row.evaluation_gate_result["outcome"] == "true" for row in evaluations)
    assert all(row.criterion_results[0]["actual"] > 3 for row in evaluations)


def test_false_gate_appearance_selection_does_not_require_downstream_length_class():
    plan, rows = gated_model()
    binding = replace(plan.rule_bindings[0], rule_selection={
        "conditions": [], "priority": 1, "specificity": 1, "is_default": False,
        "appearance_conditions": [{"factor_id": "F_APPR_REGION_REQ", "operator": "EQ", "value": "非外观"}],
        "appearance_operands": ["delta", "length"],
    })
    plan = replace(plan, rule_bindings=[binding])
    measured = [replace(row, value=0.2, quality={**row.quality, "appearance_class": "non_appearance"})
                if row.geometric_id == "G_WALL_TRANS_DT" else replace(row, value=None, status="error", reason="length unavailable")
                for row in rows]
    evaluated, _ = EvaluationEngine().evaluate(measured, plan)
    assert len(evaluated) == 2
    assert all(row.outcome == "not_applicable" and row.criterion_results == [] for row in evaluated)


@pytest.mark.parametrize("failure", ["missing", "unavailable", "unit", "unscoped"])
def test_unknown_gate_never_becomes_zero_or_pass_and_stays_in_its_instance(failure):
    plan, rows = gated_model()
    changed = []
    for row in rows:
        if row.geometric_id == "G_WALL_TRANS_DT" and row.measurement_instance_id == "first":
            if failure == "missing":
                continue
            if failure == "unavailable":
                row = replace(row, status="not_applicable", value=None, reason="no sample")
            elif failure == "unit":
                row = replace(row, unit="degree")
            else:
                row = replace(row, quality={})
        changed.append(row)
    evaluations, sources = EvaluationEngine().evaluate(changed, plan)
    by_instance = {sources[row.evaluation_id]["measurement_instance_id"]: row for row in evaluations}
    first = by_instance["first"]
    assert first.outcome == "indeterminate"
    assert first.criterion_results == []
    assert first.evaluation_gate_result["outcome"] == "indeterminate"
    assert by_instance["second"].outcome == "pass"


def test_gated_transition_uses_explicit_global_count_without_an_extra_summary_case():
    plan, rows = gated_model()
    binding = plan.rule_bindings[0]
    count = GeometricRuleOperand("steps", "G_WALL_STEP_COUNT", aggregation="maximum", feature_refs=["wall"])
    criterion = {"criterion_id": "no_step", "expression": {"operand": "steps"},
                 "comparator": "EQ", "threshold": 0, "result_unit": "count"}
    binding = replace(binding, additional_operands=[*binding.additional_operands, count],
                      acceptance_criteria_json=[[binding.acceptance_criteria_json[0][0], criterion]])
    plan = replace(plan, rule_bindings=[binding], plan_fragments=[replace(plan.plan_fragments[0],
        items=[GeometricRequest("wall", ["G_WALL_TRANS_DT", "G_WALL_TRANS_LEN", "G_WALL_STEP_COUNT"], {})])])
    summary = replace(rows[0], result_id="global-steps", geometric_id="G_WALL_STEP_COUNT",
                      value=1, unit="count", quality={"measurement_instance_id": "feature_summary"})
    evaluations, sources = EvaluationEngine().evaluate([*rows, summary], plan)
    assert {sources[row.evaluation_id]["measurement_instance_id"] for row in evaluations} == {"first", "second"}
    assert all(row.outcome == "fail" for row in evaluations)
    assert all(row.criterion_results[1]["measurement_ids"] == ["global-steps"] for row in evaluations)


@pytest.mark.parametrize("delta,steps,length,outcome", [
    (1.0, 0, None, "pass"), (1.0, 1, None, "indeterminate"),
    (1.0, None, 4.0, "indeterminate"), (1.0, 1, 2.0, "fail"),
    (0.2, None, None, "not_applicable"), (1.0, 1, 4.0, "pass"),
])
def test_transition_gate_then_whole_wall_step_branch(delta, steps, length, outcome):
    plan, rows = gated_model()
    binding = plan.rule_bindings[0]
    nonzero = {"criterion_id": "has_steps", "expression": {"operand": "steps"},
               "comparator": "NE", "threshold": 0, "result_unit": "count"}
    zero = {**nonzero, "criterion_id": "no_steps", "comparator": "EQ"}
    binding = replace(binding, geometric_id="G_WALL_STEP_COUNT", operand_alias="steps", aggregation="maximum",
                      operator="!=", expression={"operand": "steps"},
                      additional_operands=[GeometricRuleOperand("delta", "G_WALL_TRANS_DT", feature_refs=["wall"]), *binding.additional_operands],
                      acceptance_criteria_json=[[nonzero, binding.acceptance_criteria_json[0][0]], [zero]])
    plan = replace(plan, rule_bindings=[binding], rules={"rule": EffectiveRule(0, "count", "fixture")},
                   plan_fragments=[replace(plan.plan_fragments[0], items=[GeometricRequest("wall",
                       ["G_WALL_TRANS_DT", "G_WALL_TRANS_LEN", "G_WALL_STEP_COUNT"], {})])])
    measured = [replace(rows[0], value=delta)]
    if length is not None:
        measured.append(replace(rows[1], value=length))
    if steps is not None:
        measured.append(replace(rows[0], result_id="global-steps", geometric_id="G_WALL_STEP_COUNT",
                                value=steps, unit="count", quality={"measurement_instance_id": "feature_summary"}))
    evaluated, _ = EvaluationEngine().evaluate(measured, plan)
    assert len(evaluated) == 1
    assert evaluated[0].outcome == outcome


def test_gated_identity_does_not_broadcast_unscoped_acceptance_measurements():
    plan, rows = gated_model()
    delta = [row for row in rows if row.geometric_id == "G_WALL_TRANS_DT"]
    shared = replace(rows[1], quality={})
    evaluations, _ = EvaluationEngine().evaluate([*delta, shared], plan)
    assert all(row.outcome == "indeterminate" for row in evaluations)
    assert all(row.evaluation_gate_result["outcome"] == "true" for row in evaluations)


def test_null_gate_keeps_legacy_serialized_binding_and_rule_hash():
    plan, rows = model()
    original = plan.rule_bindings[0]
    restored = type(original).from_dict(original.to_dict())
    assert restored.evaluation_gate_json is None
    assert "evaluation_gate_json" not in restored.to_dict()
    original_results, _ = EvaluationEngine().evaluate(rows, plan)
    explicit_null_results, _ = EvaluationEngine().evaluate(rows, replace(plan,
        rule_bindings=[replace(restored, evaluation_gate_json=None)]))
    assert [row.to_dict() for row in original_results] == [row.to_dict() for row in explicit_null_results]
