import json
from pathlib import Path

from tools.dfm.contracts import (
    ArtifactRecord,
    FeatureRecord,
    GeometryRef,
    PlanRecord,
    ProjectManifest,
    RegionRecord,
)
from tools.dfm.issue_types import classify_issue_type
from tools.dfm.viewer import (
    materialize_discovery_viewer_manifest,
    materialize_preview_manifest,
    materialize_viewer_manifest,
)


def test_issue_type_labels_cover_all_injection_checks():
    expected_labels = {
        "C_BOSS_DRAFT": "普通凸台拔模角问题",
        "C_BOSS_ROOT_FILLET": "普通凸台根部圆角问题",
        "C_HOLE_BOTTOM_THK": "一般孔孔底厚度问题",
        "C_HOLE_CLEARANCE": "一般孔孔边距/孔间距问题",
        "C_HOLE_DRAFT": "一般孔孔壁拔模角问题",
        "C_HOLE_THIN_STEEL": "一般孔薄钢问题",
        "C_RIB_DRAFT": "加强筋拔模角问题",
        "C_RIB_HEIGHT_RATIO": "加强筋高度比问题",
        "C_RIB_ROOT_FILLET": "加强筋根部圆角问题",
        "C_RIB_THK_RATIO": "加强筋厚度比问题",
        "C_SCREW_BOSS_BOTTOM_THK": "螺钉柱孔底厚度问题",
        "C_SCREW_BOSS_DRAFT": "螺钉柱拔模角问题",
        "C_SCREW_BOSS_FILLET": "螺钉柱圆角问题",
        "C_SCREW_BOSS_HOLE_DEPTH": "螺钉柱孔芯深度问题",
        "C_SCREW_BOSS_THIN_STEEL": "螺钉柱薄钢问题",
        "C_SCREW_BOSS_WALL_THK": "螺钉柱柱壁问题",
        "C_WALL_DRAFT": "主体壁拔模角问题",
        "C_WALL_FILLET": "主体壁圆角问题",
        "C_WALL_THK_RANGE": "主体壁厚范围问题",
        "C_WALL_THK_TRANSITION": "主体壁厚过渡问题",
    }

    for check_id, label in expected_labels.items():
        assert classify_issue_type(check_id, None) == (check_id, label)


def _artifact(
    project_dir: Path, kind: str, filename: str, payload: dict
) -> ArtifactRecord:
    path = project_dir / "runs" / "run_1" / "artifacts" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ArtifactRecord(
        f"artifact_{kind}",
        kind,
        path.relative_to(project_dir).as_posix(),
        "application/json",
        "now",
    )


def test_viewer_manifest_maps_failed_evaluation_to_geometry_refs(tmp_path):
    artifacts = [
        _artifact(
            tmp_path,
            "render_scene",
            "render_scene.json",
            {"schema_version": 2, "primitives": []},
        ),
        _artifact(
            tmp_path,
            "topology_map",
            "topology_map.json",
            {"schema_version": 2, "faces": []},
        ),
        _artifact(
            tmp_path,
            "measurements",
            "measurements.json",
            {
                "input_sha256": "a" * 64,
                "measurements": [
                    {
                        "measurement_id": "measurement-draft-minimum",
                        "geometry_refs": [
                            {"kind": "face", "index": 7, "input_sha256": "a" * 64}
                        ],
                    }
                ],
            },
        ),
        _artifact(
            tmp_path,
            "features",
            "features.json",
            {
                "features": [
                    {
                        "feature_id": "feature-drilled-hole-face-7",
                        "kind": "drilled_hole",
                        "subtype": "through_hole",
                        "confidence": 0.97,
                        "geometry_refs": [
                            {
                                "kind": "face",
                                "index": 7,
                                "input_sha256": "a" * 64,
                            }
                        ],
                        "parameters": {
                            "diameter_mm": 6.0,
                            "depth_mm": 12.0,
                        },
                        "method": "analysis_situs_recognize_drill_holes",
                        "diagnostics": {
                            "full_circle_boundary_required": True,
                        },
                    }
                ]
            },
        ),
        _artifact(
            tmp_path,
            "evaluations",
            "evaluations.json",
            {
                "evaluations": [
                    {
                        "evaluation_id": "evaluation-draft",
                        "outcome": "fail",
                        "rule_id": "min_draft_deg",
                        "check_id": "check.main_wall_minimum_draft",
                        "metric_id": "injection.geometry.draft",
                        "measurement_ids": ["measurement-draft-minimum"],
                        "actual": 0.4,
                        "expected": 1.0,
                        "operator": ">=",
                    }
                ]
            },
        ),
    ]
    plan = PlanRecord(
        "plan_1",
        "step",
        ["occt_cpp"],
        "ready",
        "now",
        process="injection",
        scope_id="injection.geometry-core",
        scope_version="4.0.0",
    )

    result = materialize_viewer_manifest(tmp_path, "run_1", plan, artifacts)

    assert result is not None
    payload = json.loads((tmp_path / result.relative_path).read_text(encoding="utf-8"))
    assert payload["contract_version"] == "hermes.dfm.viewer/v2"
    assert payload["scene_path"] == "render_scene.json"
    assert "mesh_path" not in payload
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["geometry_refs"] == [
        {"kind": "face", "index": 7, "input_sha256": "a" * 64}
    ]
    assert payload["issues"][0]["check_id"] == "check.main_wall_minimum_draft"
    assert payload["issues"][0]["issue_type_label"] == "拔模角问题"
    assert payload["issue_type_counts"] == [
        {
            "issue_type_id": "check.main_wall_minimum_draft",
            "label": "拔模角问题",
            "count": 1,
        }
    ]
    assert payload["feature_count"] == 1
    assert payload["features"][0] == {
        "feature_id": "feature-drilled-hole-face-7",
        "kind": "drilled_hole",
        "subtype": "through_hole",
        "confidence": 0.97,
        "geometry_refs": [{"kind": "face", "index": 7, "input_sha256": "a" * 64}],
        "parameters": {"diameter_mm": 6.0, "depth_mm": 12.0},
        "method": "analysis_situs_recognize_drill_holes",
        "diagnostics": {"full_circle_boundary_required": True},
    }


def test_preview_manifest_renders_before_rule_evaluation(tmp_path):
    artifacts = [
        _artifact(
            tmp_path,
            "render_scene",
            "render_scene.json",
            {"schema_version": 2, "primitives": []},
        ),
        _artifact(
            tmp_path,
            "topology_map",
            "topology_map.json",
            {"schema_version": 2, "faces": []},
        ),
    ]

    result = materialize_preview_manifest(tmp_path, "run_1", "b" * 64, artifacts)

    assert result is not None
    payload = json.loads((tmp_path / result.relative_path).read_text(encoding="utf-8"))
    assert payload["status"] == "preview"
    assert payload["input_sha256"] == "b" * 64
    assert payload["issue_count"] == 0
    assert payload["issues"] == []
    assert payload["issue_type_counts"] == []
    assert payload["feature_count"] == 0
    assert payload["features"] == []


def test_discovery_manifest_exposes_editable_feature_regions(tmp_path):
    scene = _artifact(tmp_path, "render_scene", "render_scene.json", {})
    topology = _artifact(tmp_path, "topology_map", "topology_map.json", {})
    geometry_ref = GeometryRef(
        "face", 7, "a" * 64, "topology-1", "face-7"
    )
    feature = FeatureRecord(
        "feature.main-wall.1",
        "main_wall",
        ["recognizer:test"],
        0.98,
        input_sha256="a" * 64,
        region_refs=["region.main-wall.1"],
    )
    region = RegionRecord(
        "region.main-wall.1",
        "a" * 64,
        "model",
        "topology_refs",
        "main_wall",
        ["recognizer:test"],
        "1",
        "b" * 64,
        geometry_refs=[geometry_ref],
        role="wall",
        feature_refs=[feature.feature_id],
    )
    manifest = ProjectManifest(
        "dfm_1",
        "part",
        "now",
        "now",
        features=[feature],
        regions=[region],
        artifacts=[scene, topology],
        capabilities={
            "geometry_discovery": {
                "inputs": {
                    "a" * 64: {
                        "artifact_refs": [scene.artifact_id, topology.artifact_id]
                    }
                }
            }
        },
        revision=4,
    )

    result = materialize_discovery_viewer_manifest(
        tmp_path, manifest, review_status="pending"
    )

    assert result is not None
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["status"] == "discovery"
    assert payload["review_status"] == "pending"
    assert payload["project_revision"] == 4
    assert payload["issues"] == []
    assert payload["features"][0]["feature_id"] == feature.feature_id
    assert payload["features"][0]["geometry_refs"] == [geometry_ref.to_dict()]


def test_viewer_manifest_uses_failed_patches_instead_of_representative_measurement_face(tmp_path):
    artifacts = [
        _artifact(tmp_path, "render_scene", "render_scene.json", {
            "render_mesh_snapshot": {"render_mesh_snapshot_id": "mesh-current"},
        }),
        _artifact(tmp_path, "topology_map", "topology_map.json", {}),
        _artifact(tmp_path, "measurements", "measurements.json", {
            "measurements": [{"measurement_id": "measure-1", "geometry_refs": [{"kind": "face", "index": 7}]}],
        }),
        _artifact(tmp_path, "features", "features.json", {"features": []}),
        _artifact(tmp_path, "evaluations", "evaluations.json", {
            "evaluations": [{
                "evaluation_id": "evaluation-1", "outcome": "fail", "rule_id": "rule-1",
                "measurement_ids": ["measure-1"],
            }],
        }),
        _artifact(tmp_path, "evidence_geometry", "evidence_geometry.json", {
            "failed_patches": [
                {
                    "evaluation_id": "evaluation-1", "render_mesh_snapshot_ref": "mesh-current",
                    "geometry_refs": [{"kind": "face", "index": 9}],
                    "triangle_refs": [{"primitive_id": "face-9", "triangle_id": 2, "render_mesh_snapshot_id": "mesh-current"}],
                },
                {
                    "evaluation_id": "evaluation-1", "render_mesh_snapshot_ref": "mesh-current",
                    "geometry_refs": [{"kind": "face", "index": 9}],
                    "triangle_refs": [{"primitive_id": "face-9", "triangle_id": 2, "render_mesh_snapshot_id": "mesh-current"}],
                },
                {
                    "evaluation_id": "evaluation-1", "render_mesh_snapshot_ref": "mesh-other",
                    "geometry_refs": [{"kind": "face", "index": 11}],
                    "triangle_refs": [{"primitive_id": "face-11", "triangle_id": 0, "render_mesh_snapshot_id": "mesh-other"}],
                },
            ],
        }),
    ]
    plan = PlanRecord("plan_1", "step", ["occt_cpp"], "ready", "now")

    result = materialize_viewer_manifest(tmp_path, "run_1", plan, artifacts)

    assert result is not None
    payload = json.loads((tmp_path / result.relative_path).read_text(encoding="utf-8"))
    assert payload["issues"][0]["geometry_refs"] == [{"kind": "face", "index": 9}]
    assert payload["issues"][0]["triangle_refs"] == [
        {"primitive_id": "face-9", "triangle_id": 2, "render_mesh_snapshot_id": "mesh-current"}
    ]
