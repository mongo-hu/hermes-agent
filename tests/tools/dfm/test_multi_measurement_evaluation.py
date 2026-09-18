import json
from dataclasses import replace
from pathlib import Path

import pytest

from tools.dfm.contracts import (
    EffectiveRule,
    MeasurementRecord,
    PlanOperation,
    PlanRecord,
    RuleBinding,
    RuleOperand,
)
from tools.dfm.errors import DFMError
from tools.dfm.evaluation import EvaluationEngine


METRIC_ID = "injection.geometry.wall_thickness"
QUANTITY_ID = "thickness_mm"


def _measurement(
    measurement_id: str,
    operation_id: str,
    value: float,
    region_ref: str,
    *,
    input_sha256: str = "a" * 64,
) -> MeasurementRecord:
    return MeasurementRecord(
        measurement_id=measurement_id,
        operation_id=operation_id,
        calculator_id="measure_wall_thickness",
        metric_id=METRIC_ID,
        quantity_id=QUANTITY_ID,
        value=value,
        unit="mm",
        status="measured",
        geometry_refs=[],
        method="occt_wall_thickness",
        algorithm_version="occt-wall-thickness-v1",
        input_sha256=input_sha256,
        region_refs=[region_ref],
    )


def _ratio_plan() -> PlanRecord:
    binding = RuleBinding(
        binding_id="binding.screw_boss.wall_ratio",
        operation_id="geometry.wall_thickness.boss",
        metric_id=METRIC_ID,
        quantity_id=QUANTITY_ID,
        rule_id="R_SCREW_BOSS_WALL_THK_001",
        operator="between",
        aggregation="identity",
        check_id="C_SCREW_BOSS_WALL_THK",
        operand_alias="boss_wall_thickness",
        region_refs=["region.screw_boss.1.wall"],
        additional_operands=[
            RuleOperand(
                alias="adjacent_main_wall_thickness",
                operation_id="geometry.wall_thickness.main",
                metric_id=METRIC_ID,
                quantity_id=QUANTITY_ID,
                aggregation="identity",
                region_refs=["region.main_wall.1.wall"],
            )
        ],
        expression={
            "op": "divide",
            "args": [
                {"operand": "boss_wall_thickness"},
                {"operand": "adjacent_main_wall_thickness"},
            ],
        },
    )
    return PlanRecord(
        plan_id="plan.multi_measurement",
        input_mode="step",
        analyzer_keys=["step"],
        status="ready",
        created_at="2026-08-24T00:00:00Z",
        process="injection",
        scope_id="injection.screw-boss",
        scope_version="1.0.0",
        rules={
            "R_SCREW_BOSS_WALL_THK_001": EffectiveRule(
                value={"lower": 0.4, "upper": 0.6},
                unit="ratio",
                source="rule-set:system@1.0.0",
                version="1",
                severity="warning",
            )
        },
        rule_bindings=[binding],
        operations=[
            PlanOperation(
                operation_id="geometry.wall_thickness.boss",
                calculator_id="measure_wall_thickness",
                metric_ids=[METRIC_ID],
                required_quantities=[QUANTITY_ID],
            ),
            PlanOperation(
                operation_id="geometry.wall_thickness.main",
                calculator_id="measure_wall_thickness",
                metric_ids=[METRIC_ID],
                required_quantities=[QUANTITY_ID],
            ),
        ],
    )


def test_multi_measurement_ratio_evaluates_one_check_once():
    plan = _ratio_plan()
    measurements = [
        _measurement(
            "measurement.boss.wall",
            "geometry.wall_thickness.boss",
            1.0,
            "region.screw_boss.1.wall",
        ),
        _measurement(
            "measurement.main.wall",
            "geometry.wall_thickness.main",
            2.0,
            "region.main_wall.1.wall",
        ),
    ]

    evaluations, provenance = EvaluationEngine().evaluate(measurements, plan)

    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.check_id == "C_SCREW_BOSS_WALL_THK"
    assert evaluation.measurement_ids == [
        "measurement.boss.wall",
        "measurement.main.wall",
    ]
    assert evaluation.actual == pytest.approx(0.5)
    assert evaluation.actual_unit == "ratio"
    assert evaluation.outcome == "pass"
    assert evaluation.severity == "warning"
    assert set(evaluation.operand_values) == {
        "boss_wall_thickness",
        "adjacent_main_wall_thickness",
    }
    assert provenance[evaluation.evaluation_id]["check_id"] == evaluation.check_id


@pytest.mark.parametrize(
    "boss,main,outcome,failed",
    [(1.0, 2.0, "pass", []), (0.7, 2.0, "fail", ["wall_min", "wall_ratio"]), (1.0, 3.0, "fail", ["wall_ratio"])],
)
def test_composite_rule_reuses_measurements_and_reports_each_clause(boss, main, outcome, failed):
    base = _ratio_plan()
    criteria = [
        {"criterion_id": "wall_min", "expression": {"operand": "boss_wall_thickness"},
         "comparator": "GTE", "threshold": 0.8, "result_unit": "mm"},
        {"criterion_id": "wall_ratio", "expression": base.rule_bindings[0].expression,
         "comparator": "GT", "threshold": 0.4, "result_unit": "ratio"},
    ]
    binding = replace(base.rule_bindings[0], expression=criteria[0]["expression"], operator=">=", acceptance_criteria_json=criteria)
    plan = replace(base, rule_bindings=[binding], rules={
        binding.rule_id: replace(base.rules[binding.rule_id], value=0.8, unit="mm", severity="high", severity_rationale="Two limits protect the boss wall.")
    })
    measurements = [
        _measurement("measurement.boss.wall", "geometry.wall_thickness.boss", boss, "region.screw_boss.1.wall"),
        _measurement("measurement.main.wall", "geometry.wall_thickness.main", main, "region.main_wall.1.wall"),
    ]
    evaluations, provenance = EvaluationEngine().evaluate(measurements, plan)
    assert len(evaluations) == 1
    result = evaluations[0]
    assert result.outcome == outcome
    assert result.severity == "high"
    assert result.severity_rationale == "Two limits protect the boss wall."
    assert result.measurement_ids == ["measurement.boss.wall", "measurement.main.wall"]
    assert [item["criterion_id"] for item in result.criterion_results if item["outcome"] == "fail"] == failed
    assert result.criterion_results[0]["region_refs"] == ["region.screw_boss.1.wall"]
    assert result.criterion_results[1]["region_refs"] == ["region.main_wall.1.wall", "region.screw_boss.1.wall"]
    assert provenance[result.evaluation_id]["criterion_results"] == result.criterion_results


def test_composite_rule_keeps_a_known_failure_when_another_operand_is_missing():
    base = _ratio_plan()
    criteria = [
        {"criterion_id": "wall_min", "expression": {"operand": "boss_wall_thickness"},
         "comparator": "GTE", "threshold": 0.8, "result_unit": "mm"},
        {"criterion_id": "wall_ratio", "expression": base.rule_bindings[0].expression,
         "comparator": "GT", "threshold": 0.4, "result_unit": "ratio"},
    ]
    binding = replace(base.rule_bindings[0], expression=criteria[0]["expression"], operator=">=", acceptance_criteria_json=criteria)
    plan = replace(base, rule_bindings=[binding], rules={binding.rule_id: replace(base.rules[binding.rule_id], value=0.8, unit="mm")})
    evaluations, _ = EvaluationEngine().evaluate([
        _measurement("measurement.boss.wall", "geometry.wall_thickness.boss", 0.7, "region.screw_boss.1.wall")
    ], plan)
    assert evaluations[0].outcome == "fail"
    assert [item["outcome"] for item in evaluations[0].criterion_results] == ["fail", "indeterminate"]


def test_composite_rule_rejects_inconsistent_pinned_primary_threshold():
    base = _ratio_plan()
    criterion = {
        "criterion_id": "wall_ratio",
        "expression": base.rule_bindings[0].expression,
        "comparator": "BETWEEN",
        "threshold": {"lower": 0.4, "upper": 0.6},
        "result_unit": "ratio",
    }
    binding = replace(base.rule_bindings[0], acceptance_criteria_json=[criterion])
    invalid = replace(base, rule_bindings=[binding], rules={
        binding.rule_id: replace(base.rules[binding.rule_id], value={"lower": 0.5, "upper": 0.6})
    })
    with pytest.raises(DFMError) as exc_info:
        EvaluationEngine().evaluate([], invalid)
    assert exc_info.value.code == "evaluation_rule_invalid"


def test_multi_measurement_binding_round_trips_and_matches_schema():
    binding = _ratio_plan().rule_bindings[0]
    payload = binding.to_dict()

    assert RuleBinding.from_dict(payload) == binding
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "tools"
        / "dfm"
        / "schemas"
        / "rule_binding.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_multi_measurement_rule_rejects_missing_reference_operand():
    plan = _ratio_plan()
    measurements = [
        _measurement(
            "measurement.boss.wall",
            "geometry.wall_thickness.boss",
            1.0,
            "region.screw_boss.1.wall",
        )
    ]

    with pytest.raises(DFMError) as exc_info:
        EvaluationEngine().evaluate(measurements, plan)

    assert exc_info.value.code == "evaluation_operand_missing"
    assert exc_info.value.details["operand_alias"] == ("adjacent_main_wall_thickness")


def test_multi_measurement_rule_rejects_cross_input_values():
    plan = _ratio_plan()
    measurements = [
        _measurement(
            "measurement.boss.wall",
            "geometry.wall_thickness.boss",
            1.0,
            "region.screw_boss.1.wall",
        ),
        _measurement(
            "measurement.main.wall",
            "geometry.wall_thickness.main",
            2.0,
            "region.main_wall.1.wall",
            input_sha256="b" * 64,
        ),
    ]

    with pytest.raises(DFMError) as exc_info:
        EvaluationEngine().evaluate(measurements, plan)

    assert exc_info.value.code == "evaluation_operand_invalid"


def test_multi_measurement_rule_rejects_division_by_zero():
    plan = _ratio_plan()
    measurements = [
        _measurement(
            "measurement.boss.wall",
            "geometry.wall_thickness.boss",
            1.0,
            "region.screw_boss.1.wall",
        ),
        _measurement(
            "measurement.main.wall",
            "geometry.wall_thickness.main",
            0.0,
            "region.main_wall.1.wall",
        ),
    ]

    with pytest.raises(DFMError) as exc_info:
        EvaluationEngine().evaluate(measurements, plan)

    assert exc_info.value.code == "evaluation_expression_invalid"
