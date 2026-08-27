import json
from pathlib import Path

from tools.dfm.contracts import ArtifactRecord, PlanRecord
from tools.dfm.viewer import materialize_preview_manifest, materialize_viewer_manifest


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
        ["occt"],
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
    assert payload["feature_count"] == 0
    assert payload["features"] == []
