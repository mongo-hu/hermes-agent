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
from tools.dfm.evidence import planner as evidence_planner
from tools.dfm.evidence.planner import plan_evidence_views, prepare_scene
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
        tmp_path, "run_1", artifacts, max_markers_per_image=4
    )
    all_artifacts = [*artifacts, *generated]

    image_artifacts = [item for item in generated if item.kind == "evidence_image"]
    assert len(image_artifacts) == 1
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
    assert geometry["schema_version"] == 3
    assert geometry["coverage_requirements"]["required_patch_ids"] == [
        patch["patch_id"]
    ]
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
    assert records["coverage"]["coverage_ratio"] == 1.0
    assert records["coverage"]["uncovered_patch_ids"] == []
    assert records["coverage"]["uncovered_sample_refs"] == []
    assert records["records"][0]["render"]["covered_patch_ids"] == [
        patch["patch_id"]
    ]
    assert records["records"][0]["render"]["markers"] == [
        {"label": "ISSUE 01", "patch_id": patch["patch_id"]}
    ]

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


def test_evidence_is_allocated_per_failed_evaluation(tmp_path):
    artifacts = _write_pipeline_inputs(tmp_path)
    evaluations_path = tmp_path / "evaluations.json"
    payload = json.loads(evaluations_path.read_text(encoding="utf-8"))
    composite = dict(payload["evaluations"][0])
    composite.update({
        "evaluation_id": "evaluation-composite-ratio",
        "operator": ">=",
        "expected": 0.75,
        "actual": 0.4,
        "expression": {
            "op": "divide",
            "args": [{"operand": "actual"}, {"operand": "reference"}],
        },
        "operand_values": {
            "actual": {
                "value": 1.2,
                "unit": "degree",
                "measurement_ids": ["measurement_draft_fixed_half_min"],
            },
            "reference": {
                "value": 3.0,
                "unit": "degree",
                "measurement_ids": ["measurement_draft_fixed_half_min"],
            },
        },
    })
    payload["evaluations"].append(composite)
    evaluations_path.write_text(json.dumps(payload), encoding="utf-8")

    generated = FieldEvidenceEngine().materialize(
        tmp_path, "run_1", artifacts, max_markers_per_image=2
    )

    images = [item for item in generated if item.kind == "evidence_image"]
    assert len(images) == 2
    records_artifact = next(
        item for item in generated if item.kind == "evidence_records"
    )
    records = json.loads(
        (tmp_path / records_artifact.relative_path).read_text(encoding="utf-8")
    )["records"]
    evaluation_ids = {item["evaluation_ids"][0] for item in records}
    assert evaluation_ids == {
        "evaluation-measurement_draft_fixed_half_min",
        "evaluation-composite-ratio",
    }
    assert len({item["artifact_ref"] for item in records}) == len(records) == 2
    assert {item["render"]["mode"] for item in records} == {"local_issue"}
    assert {item["render"]["presentation_mode"] for item in records} == {"local"}


def test_evidence_covers_every_failure_beyond_the_legacy_limit(tmp_path):
    artifacts = _write_pipeline_inputs(tmp_path)
    evaluations_path = tmp_path / "evaluations.json"
    payload = json.loads(evaluations_path.read_text(encoding="utf-8"))
    source = payload["evaluations"][0]
    payload["evaluations"] = [
        {**source, "evaluation_id": f"evaluation-many-{index:02d}"}
        for index in range(15)
    ]
    evaluations_path.write_text(json.dumps(payload), encoding="utf-8")

    generated = FieldEvidenceEngine().materialize(
        tmp_path,
        "run_1",
        artifacts,
        max_markers_per_image=4,
    )

    images = [item for item in generated if item.kind == "evidence_image"]
    records_artifact = next(
        item for item in generated if item.kind == "evidence_records"
    )
    payload = json.loads(
        (tmp_path / records_artifact.relative_path).read_text(encoding="utf-8")
    )
    assert len(images) == 15
    assert len(payload["records"]) == 15
    assert len({item["artifact_ref"] for item in payload["records"]}) == 15
    assert payload["coverage"]["required_patch_count"] == 15
    assert payload["coverage"]["covered_patch_count"] == 15
    assert payload["coverage"]["coverage_ratio"] == 1.0
    assert payload["coverage"]["uncovered_patch_ids"] == []
    assert payload["coverage"]["uncovered_sample_refs"] == []


def test_disconnected_failed_samples_become_separate_required_patches():
    geometry_ref = {
        "kind": "face",
        "index": 1,
        "input_sha256": "a" * 64,
        "topology_snapshot_id": "topology-1",
        "entity_id": "face-1",
    }
    scene = {
        "primitives": [{
            "primitive_id": "body-1",
            "render_mesh_snapshot_id": "mesh-1",
            "vertices": [
                [0, 0, 0],
                [1, 0, 0],
                [0, 1, 0],
                [10, 0, 0],
                [11, 0, 0],
                [10, 1, 0],
            ],
            "triangles": [[0, 1, 2], [3, 4, 5]],
        }]
    }
    field = {
        "scene_ref": "scene-1",
        "topology_map_ref": "map-1",
        "topology_snapshot_ref": "topology-1",
        "render_mesh_snapshot_ref": "mesh-1",
        "samples": [
            {
                "sample_id": "sample-a",
                "value": 0.5,
                "point": [0.25, 0.25, 0],
                "surface_normal": [0, 0, 1],
                "geometry_ref": geometry_ref,
            },
            {
                "sample_id": "sample-b",
                "value": 0.4,
                "point": [10.25, 0.25, 0],
                "surface_normal": [0, 0, 1],
                "geometry_ref": geometry_ref,
            },
        ],
        "cells": [
            {
                "cell_id": "cell-a",
                "sample_ids": ["sample-a"],
                "geometry_ref": geometry_ref,
                "triangle_ref": {
                    "primitive_id": "body-1",
                    "triangle_id": 0,
                    "render_mesh_snapshot_id": "mesh-1",
                },
            },
            {
                "cell_id": "cell-b",
                "sample_ids": ["sample-b"],
                "geometry_ref": geometry_ref,
                "triangle_ref": {
                    "primitive_id": "body-1",
                    "triangle_id": 1,
                    "render_mesh_snapshot_id": "mesh-1",
                },
            },
        ],
    }

    patches = FieldEvidenceEngine._failed_patches(
        {"evaluation_id": "evaluation-1", "operator": ">=", "expected": 1.0},
        {"measurement_id": "measurement-1"},
        field_ref="field-1",
        field=field,
        scene=scene,
        comparison=lambda actual, expected: actual >= expected,
    )

    assert len(patches) == 2
    assert {sample for patch in patches for sample in patch["sample_refs"]} == {
        "field-1:sample-a",
        "field-1:sample-b",
    }


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
def test_coverage_view_planning_is_stable_for_rotated_and_parallel_geometry(
    pull_direction, surface_normal
):
    scene = {
        "primitives": [{
            "primitive_id": "body-1",
            "render_mesh_snapshot_id": "mesh-1",
            "vertices": [[-4, -2, -1], [4, -2, -1], [4, 2, 1], [-4, 2, 1]],
            "triangles": [[0, 1, 2], [0, 2, 3]],
        }]
    }
    patch = {
        "patch_id": "patch-1",
        "sample_ids": ["sample-1"],
        "focus_point": [3, 1, 0.5],
        "surface_normal": surface_normal,
        "bounds": {"minimum": [-4, -2, -1], "maximum": [4, 2, 1]},
        "triangle_refs": [
            {
                "primitive_id": "body-1",
                "render_mesh_snapshot_id": "mesh-1",
                "triangle_id": 0,
            }
        ],
    }

    prepared = prepare_scene(scene)
    first = plan_evidence_views(
        prepared, [patch], pull_direction, max_markers_per_image=10
    )
    second = plan_evidence_views(
        prepared, [patch], pull_direction, max_markers_per_image=10
    )

    assert first == second
    assert len(first) == 1
    assert first[0].patch_ids == ("patch-1",)
    view = first[0].view
    assert sum(item * item for item in view["basis_u"]) == pytest.approx(1)
    assert sum(item * item for item in view["basis_v"]) == pytest.approx(1)
    assert sum(item * item for item in view["basis_d"]) == pytest.approx(1)


def test_coverage_view_planning_extracts_each_patch_geometry_once(monkeypatch):
    scene = {
        "primitives": [{
            "primitive_id": "body-1",
            "render_mesh_snapshot_id": "mesh-1",
            "vertices": [[-4, -2, -1], [4, -2, -1], [4, 2, 1], [-4, 2, 1]],
            "triangles": [[0, 1, 2], [0, 2, 3]],
        }]
    }
    patches = [
        {
            "patch_id": f"patch-{index:02d}",
            "sample_ids": [f"sample-{index:02d}"],
            "focus_point": [float(index), 1, 0.5],
            "surface_normal": [0, 0, 1],
            "bounds": {"minimum": [-4, -2, -1], "maximum": [4, 2, 1]},
            "triangle_refs": [{
                "primitive_id": "body-1",
                "render_mesh_snapshot_id": "mesh-1",
                "triangle_id": index % 2,
            }],
        }
        for index in range(12)
    ]
    prepared = prepare_scene(scene)
    original = evidence_planner._patch_points
    calls: list[str] = []

    def counted_patch_points(prepared_scene, patch):
        calls.append(str(patch["patch_id"]))
        return original(prepared_scene, patch)

    monkeypatch.setattr(evidence_planner, "_patch_points", counted_patch_points)

    plans = plan_evidence_views(
        prepared,
        patches,
        [0, 0, 1],
        max_markers_per_image=3,
    )

    assert sorted(calls) == sorted(patch["patch_id"] for patch in patches)
    assert len(calls) == len(patches)
    assert {
        patch_id for plan in plans for patch_id in plan.patch_ids
    } == {patch["patch_id"] for patch in patches}


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
