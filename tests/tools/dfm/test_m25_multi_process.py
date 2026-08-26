import json

from tools.dfm.contracts import ArtifactRecord, MeasurementRecord, PlanRecord
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.findings import materialize_evaluated_findings
from tools.dfm.geometry.brep.checks import resolve_brep_check


def test_step_operations_resolve_through_format_independent_brep_registry():
    assert resolve_brep_check("measure_wall_thickness") == "thickness"
    assert resolve_brep_check("measure_draft") == "draft"


def test_die_casting_topology_gate_has_process_specific_evaluation_and_finding(
    tmp_path,
):
    measurements = [
        MeasurementRecord(
            "measurement-valid-brep",
            "geometry.topology",
            "inspect_topology",
            "geometry.model",
            "valid_brep",
            False,
            None,
            "measured",
            [],
            "pythonocc_topology",
            "pythonocc-objective-v4",
            "c" * 64,
        )
    ]
    plan = PlanRecord(
        "plan_1",
        "step",
        ["step"],
        "ready",
        "now",
        process="die_casting",
        scope_id="die_casting.topology-baseline",
        scope_version="1.0.0",
    )
    evaluations, _ = EvaluationEngine().evaluate(measurements, plan)

    assert any(item.quantity_id == "valid_brep" for item in measurements)
    assert len(evaluations) == 1
    assert evaluations[0].rule_id == "valid_brep_required"
    assert evaluations[0].measurement_ids == [
        next(item.measurement_id for item in measurements if item.quantity_id == "valid_brep")
    ]
    assert evaluations[0].outcome == "fail"

    measurement_path = tmp_path / "measurements.json"
    measurement_path.write_text(
        json.dumps(
            {
                "input_sha256": "c" * 64,
                "process": "die_casting",
                "measurements": [item.to_dict() for item in measurements],
            }
        ),
        encoding="utf-8",
    )
    evaluation_path = tmp_path / "evaluations.json"
    evaluation_path.write_text(
        json.dumps(
            {
                "process": "die_casting",
                "evaluations": [item.to_dict() for item in evaluations],
            }
        ),
        encoding="utf-8",
    )
    findings = materialize_evaluated_findings(
        tmp_path,
        [
            ArtifactRecord(
                "m", "measurements", "measurements.json", "application/json", "now"
            ),
            ArtifactRecord(
                "e", "evaluations", "evaluations.json", "application/json", "now"
            ),
        ],
    )

    assert findings[0].rule_refs[0] == "valid_brep_required@1.0.0"
