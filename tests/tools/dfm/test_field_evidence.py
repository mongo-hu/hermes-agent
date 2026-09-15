import json
from pathlib import Path

import pytest
from PIL import Image

from tools.dfm.contracts import (
    ArtifactRecord,
    EffectiveRule,
    GeometryRef,
    MeasurementRecord,
    PlanOperation,
    PlanRecord,
    RuleBinding,
)
from tools.dfm.errors import DFMError
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.evidence import FieldEvidenceEngine
from tools.dfm.evidence.field_engine import _adaptive_views
from tools.dfm.findings import materialize_evaluated_findings


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "dfm" / "nx"


def test_rule_binding_evaluates_nx_measurement_without_diagnostic_hint():
    measurement = MeasurementRecord(
        measurement_id="measurement_draft_fixed_half_min",
        operation_id="draft.fixed_half",
        calculator_id="measure_draft",
        metric_id="dc.geometry.draft.fixed_half",
        quantity_id="draft_angle_deg",
        value=1.2,
        unit="degree",
        status="measured",
        geometry_refs=[],
        method="nx_open_draft_analysis",
        algorithm_version="nx-draft-1",
        input_sha256="a" * 64,
        feature_refs=["feature.screw_boss.003"],
        region_refs=["region.screw_boss.003.outer_wall"],
    )
    plan = PlanRecord(
        "plan_1",
        "parasolid",
        ["parasolid"],
        "ready",
        "now",
        process="die_casting",
        rules={
            "die_casting.min_draft.fixed_half": EffectiveRule(
                1.5, "degree", "approved_rule_set", "3"
            )
        },
        rule_bindings=[
            RuleBinding(
                "binding.draft.fixed_half",
                "draft.fixed_half",
                "dc.geometry.draft.fixed_half",
                "draft_angle_deg",
                "die_casting.min_draft.fixed_half",
                ">=",
                "minimum",
                feature_refs=["feature.screw_boss.003"],
                region_refs=["region.screw_boss.003.outer_wall"],
            )
        ],
        operations=[
            PlanOperation(
                "draft.fixed_half",
                "measure_draft",
                metric_ids=["dc.geometry.draft.fixed_half"],
                required_quantities=["draft_angle_deg"],
            )
        ],
    )

    evaluations, provenance = EvaluationEngine().evaluate([measurement], plan)

    assert evaluations[0].outcome == "fail"
    assert evaluations[0].expected == 1.5
    assert evaluations[0].rule_version == "3"
    assert evaluations[0].feature_refs == ["feature.screw_boss.003"]
    assert evaluations[0].region_refs == ["region.screw_boss.003.outer_wall"]
    assert provenance[evaluations[0].evaluation_id]["binding_id"] == (
        "binding.draft.fixed_half"
    )
    assert PlanRecord.from_dict(plan.to_dict()).rule_bindings == plan.rule_bindings


def test_failed_scalar_field_renders_precise_evidence_and_finding(tmp_path):
    artifacts = _write_pipeline_inputs(tmp_path)

    generated = FieldEvidenceEngine().materialize(
        tmp_path, "run_1", artifacts, max_images=4
    )
    all_artifacts = [*artifacts, *generated]

    image_artifacts = [item for item in generated if item.kind == "evidence_image"]
    assert len(image_artifacts) == 3
    image_artifact = image_artifacts[0]
    with Image.open(tmp_path / image_artifact.relative_path) as image:
        red_pixels = sum(
            1
            for red, green, blue in image.convert("RGB").get_flattened_data()
            if red > 180 and green < 100 and blue < 100
        )
    assert red_pixels > 500

    geometry_artifact = next(
        item for item in generated if item.kind == "evidence_geometry"
    )
    geometry = json.loads(
        (tmp_path / geometry_artifact.relative_path).read_text(encoding="utf-8")
    )
    patch = geometry["failed_patches"][0]
    assert patch["sample_ids"] == ["sample-1", "sample-2"]
    assert patch["triangle_refs"] == [
        {"primitive_id": "body-1", "triangle_id": 0, "render_mesh_snapshot_id": "mesh_b7904fbd1a0dfc0c"}
    ]
    assert patch["geometry_refs"] == [
        {"kind": "face", "index": 17, "input_sha256": "a" * 64, "topology_snapshot_id": "topology_ba5565e33756d25", "entity_id": "face_000017"}
    ]
    assert patch["surface_normal"] == pytest.approx(
        [0.9998871487923587, 0, 0.015022971739553945]
    )
    assert patch["feature_refs"] == ["feature.screw_boss.003"]

    records_artifact = next(
        item for item in generated if item.kind == "evidence_records"
    )
    records = json.loads(
        (tmp_path / records_artifact.relative_path).read_text(encoding="utf-8")
    )
    assert records["records"][0]["evaluation_ids"] == [
        "evaluation-measurement_draft_fixed_half_min"
    ]
    assert records["records"][0]["artifact_ref"] == image_artifact.artifact_id
    assert records["records"][0]["feature_refs"] == ["feature.screw_boss.003"]
    assert [item["render"]["view_id"] for item in records["records"]] == [
        "pull",
        "surface",
        "side",
    ]
    directions = [item["render"]["camera_direction"] for item in records["records"]]
    assert abs(directions[0][2]) == pytest.approx(1)
    assert all(
        abs(sum(left[i] * right[i] for i in range(3))) < 0.999
        for index, left in enumerate(directions)
        for right in directions[index + 1 :]
    )

    jsonschema = pytest.importorskip("jsonschema")
    schema_root = Path(__file__).resolve().parents[3] / "tools" / "dfm" / "schemas"
    for payload, schema_name in (
        (geometry, "evidence_geometry.schema.json"),
        (records, "evidence_record.schema.json"),
    ):
        schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)

    finding = materialize_evaluated_findings(tmp_path, all_artifacts)[0]
    assert finding.evidence_refs == [
        item["evidence_id"] for item in records["records"]
    ]
    assert finding.measurement_ids == ["measurement_draft_fixed_half_min"]
    assert finding.feature_refs == ["feature.screw_boss.003"]


def test_field_evidence_rejects_cross_run_scene(tmp_path):
    artifacts = _write_pipeline_inputs(tmp_path)
    scene_artifact = next(item for item in artifacts if item.kind == "render_scene")
    scene_path = tmp_path / scene_artifact.relative_path
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    scene["run_id"] = "run_other"
    scene_path.write_text(json.dumps(scene), encoding="utf-8")

    with pytest.raises(DFMError) as exc_info:
        FieldEvidenceEngine().materialize(tmp_path, "run_1", artifacts)

    assert exc_info.value.code == "evidence_field_invalid"


def test_field_evidence_rejects_retriangulated_scene_snapshot(tmp_path):
    artifacts = _write_pipeline_inputs(tmp_path)
    scene_artifact = next(item for item in artifacts if item.kind == "render_scene")
    scene_path = tmp_path / scene_artifact.relative_path
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    scene["render_mesh_snapshot"]["render_mesh_snapshot_id"] = "mesh_other"
    scene_path.write_text(json.dumps(scene), encoding="utf-8")

    with pytest.raises(DFMError) as exc_info:
        FieldEvidenceEngine().materialize(tmp_path, "run_1", artifacts)

    assert exc_info.value.code == "evidence_snapshot_mismatch"


@pytest.mark.parametrize(
    ("pull_direction", "surface_normal"),
    [
        ([1, 2, 3], [-2, 1, 0.5]),
        ([0, 0, 1], [0, 0, 1]),
    ],
)
def test_adaptive_views_stay_distinct_for_rotated_and_parallel_geometry(
    pull_direction, surface_normal
):
    scene = {
        "primitives": [{
            "vertices": [[-4, -2, -1], [4, -2, -1], [4, 2, 1], [-4, 2, 1]],
            "triangles": [[0, 1, 2], [0, 2, 3]],
        }]
    }
    patch = {
        "focus_point": [3, 1, 0.5],
        "surface_normal": surface_normal,
    }

    views = _adaptive_views(scene, patch, pull_direction)

    assert [item["id"] for item in views] == ["pull", "surface", "side"]
    directions = [item["basis_d"] for item in views]
    assert all(
        abs(sum(left[i] * right[i] for i in range(3))) < 0.999
        for index, left in enumerate(directions)
        for right in directions[index + 1 :]
    )
    for view in views:
        assert sum(item * item for item in view["basis_u"]) == pytest.approx(1)
        assert sum(item * item for item in view["basis_v"]) == pytest.approx(1)
        assert sum(item * item for item in view["basis_d"]) == pytest.approx(1)


def _write_pipeline_inputs(tmp_path: Path) -> list[ArtifactRecord]:
    fixture_files = {
        "field_draft_fixed_half": ("scalar_field", "task_contract_scalar_field.json"),
        "scene_golden_part": ("render_scene", "task_contract_render_scene.json"),
        "topology_golden_part": ("topology_map", "task_contract_topology_map.json"),
    }
    artifacts = []
    for artifact_id, (kind, fixture_name) in fixture_files.items():
        target = tmp_path / fixture_name
        target.write_text(
            (FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        artifacts.append(
            ArtifactRecord(artifact_id, kind, target.name, "application/json", "now")
        )

    measurements = tmp_path / "measurements.json"
    measurements.write_text(
        (FIXTURE_ROOT / "task_contract_measurements.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    artifacts.append(
        ArtifactRecord(
            "measurements", "measurements", measurements.name, "application/json", "now"
        )
    )
    evaluations = tmp_path / "evaluations.json"
    evaluations.write_text(
        json.dumps({
            "schema_version": 1,
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "evaluations": [{
                "evaluation_id": "evaluation-measurement_draft_fixed_half_min",
                "operation_id": "draft.fixed_half",
                "metric_id": "dc.geometry.draft.fixed_half",
                "measurement_ids": ["measurement_draft_fixed_half_min"],
                "feature_refs": ["feature.screw_boss.003"],
                "region_refs": ["region.fixed_half"],
                "rule_id": "die_casting.min_draft.fixed_half",
                "rule_version": "3",
                "rule_hash": "c" * 64,
                "operator": ">=",
                "expected": 1.5,
                "actual": 1.2,
                "outcome": "fail",
                "expression": {"operand": "actual"},
                "operand_values": {
                    "actual": {
                        "value": 1.2,
                        "unit": "degree",
                        "aggregation": "minimum",
                        "measurement_ids": ["measurement_draft_fixed_half_min"],
                    }
                },
            }]
        }),
        encoding="utf-8",
    )
    artifacts.append(
        ArtifactRecord(
            "evaluations", "evaluations", evaluations.name, "application/json", "now"
        )
    )
    return artifacts
