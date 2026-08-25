from dataclasses import replace
import json
from pathlib import Path

import pytest

from tools.dfm.contracts import (
    BoundingBox,
    EffectiveRule,
    MeasurementRecord,
    PlanOperation,
    PlanRecord,
    RegionRecord,
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
    region_ref: str | None,
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
        region_refs=[region_ref] if region_ref else [],
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
    assert set(evaluation.operand_values) == {
        "boss_wall_thickness",
        "adjacent_main_wall_thickness",
    }
    assert provenance[evaluation.evaluation_id]["check_id"] == evaluation.check_id


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


def test_unscoped_objective_measurements_match_explicit_whole_model_fallback():
    plan = replace(
        _ratio_plan(),
        regions=[
            RegionRecord(
                region_id=region_id,
                input_sha256="a" * 64,
                coordinate_system="model",
                mode="whole_model",
                semantic_label="ordinary.whole_model",
                source_refs=["discovery.snapshot"],
                version="1",
                content_sha256="b" * 64,
            )
            for region_id in (
                "region.screw_boss.1.wall",
                "region.main_wall.1.wall",
            )
        ],
    )
    measurements = [
        _measurement(
            "measurement.boss.wall",
            "geometry.wall_thickness.boss",
            1.0,
            None,
        ),
        _measurement(
            "measurement.main.wall",
            "geometry.wall_thickness.main",
            2.0,
            None,
        ),
    ]

    evaluations, _ = EvaluationEngine().evaluate(measurements, plan)

    assert evaluations[0].actual == pytest.approx(0.5)
    assert set(evaluations[0].region_refs) == {
        "region.screw_boss.1.wall",
        "region.main_wall.1.wall",
    }


def test_unscoped_objective_measurement_cannot_match_concrete_region():
    plan = replace(
        _ratio_plan(),
        regions=[
            RegionRecord(
                region_id=region_id,
                input_sha256="a" * 64,
                coordinate_system="model",
                mode="bbox",
                semantic_label="recognized.wall",
                source_refs=["discovery.snapshot"],
                version="1",
                content_sha256="b" * 64,
                bbox=BoundingBox(
                    minimum=[0.0, 0.0, 0.0],
                    maximum=[1.0, 1.0, 1.0],
                ),
            )
            for region_id in (
                "region.screw_boss.1.wall",
                "region.main_wall.1.wall",
            )
        ],
    )

    with pytest.raises(DFMError) as exc_info:
        EvaluationEngine().evaluate(
            [
                _measurement(
                    "measurement.boss.wall",
                    "geometry.wall_thickness.boss",
                    1.0,
                    None,
                )
            ],
            plan,
        )

    assert exc_info.value.code == "evaluation_operand_missing"
