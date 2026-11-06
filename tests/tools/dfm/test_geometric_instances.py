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
