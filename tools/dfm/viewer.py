"""Materialize a small desktop contract for the interactive OCCT DFM viewer."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .contracts import ArtifactRecord, PlanRecord
from .errors import DFMError
from .issue_types import classify_issue_type, summarize_issue_types


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read(project_dir: Path, artifact: ArtifactRecord) -> dict[str, Any]:
    try:
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DFMError(
            "viewer_input_invalid",
            f"The {artifact.kind} artifact cannot be used by the DFM viewer.",
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            "viewer_input_invalid",
            f"The {artifact.kind} artifact must contain an object.",
        )
    return payload


def materialize_viewer_manifest(
    project_dir: Path,
    run_id: str,
    plan: PlanRecord,
    artifacts: list[ArtifactRecord],
) -> ArtifactRecord | None:
    """Link the shared render scene, findings and topology for Desktop."""

    by_kind = {item.kind: item for item in artifacts}
    required = {
        "render_scene",
        "topology_map",
        "measurements",
        "features",
        "evaluations",
    }
    if not required.issubset(by_kind):
        return None

    measurements_payload = _read(project_dir, by_kind["measurements"])
    features_payload = _read(project_dir, by_kind["features"])
    evaluations_payload = _read(project_dir, by_kind["evaluations"])
    scene_payload = _read(project_dir, by_kind["render_scene"])
    mesh_snapshot_id = (
        scene_payload.get("render_mesh_snapshot", {}).get("render_mesh_snapshot_id")
        if isinstance(scene_payload.get("render_mesh_snapshot"), dict)
        else None
    )
    patches_by_evaluation: dict[str, list[dict[str, Any]]] = {}
    if "evidence_geometry" in by_kind:
        evidence_payload = _read(project_dir, by_kind["evidence_geometry"])
        for patch in evidence_payload.get("failed_patches", []):
            if not isinstance(patch, dict) or not patch.get("evaluation_id"):
                continue
            patch_snapshot = patch.get("render_mesh_snapshot_ref")
            if mesh_snapshot_id and patch_snapshot and patch_snapshot != mesh_snapshot_id:
                continue
            patches_by_evaluation.setdefault(str(patch["evaluation_id"]), []).append(patch)
    measurements = {
        str(item.get("measurement_id")): item
        for item in measurements_payload.get("measurements", [])
        if isinstance(item, dict) and item.get("measurement_id")
    }

    issues: list[dict[str, Any]] = []
    for evaluation in evaluations_payload.get("evaluations", []):
        if not isinstance(evaluation, dict) or evaluation.get("outcome") != "fail":
            continue
        measurement_ids = [str(item) for item in evaluation.get("measurement_ids", [])]
        linked = [
            measurements[item] for item in measurement_ids if item in measurements
        ]
        patches = patches_by_evaluation.get(str(evaluation.get("evaluation_id") or ""), [])
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, Any]] = set()
        for source in patches or linked:
            for ref in source.get("geometry_refs", []):
                if not isinstance(ref, dict):
                    continue
                key = (str(ref.get("kind") or ""), ref.get("index"))
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
        triangle_refs: list[dict[str, Any]] = []
        seen_triangles: set[tuple[str, int]] = set()
        for patch in patches:
            for ref in patch.get("triangle_refs", []):
                if not isinstance(ref, dict):
                    continue
                primitive_id = ref.get("primitive_id")
                triangle_id = ref.get("triangle_id")
                ref_snapshot = ref.get("render_mesh_snapshot_id")
                if (
                    not isinstance(primitive_id, str)
                    or not primitive_id
                    or not isinstance(triangle_id, int)
                    or triangle_id < 0
                    or (mesh_snapshot_id and ref_snapshot and ref_snapshot != mesh_snapshot_id)
                ):
                    continue
                key = (primitive_id, triangle_id)
                if key in seen_triangles:
                    continue
                seen_triangles.add(key)
                triangle_refs.append(ref)
        metric_id = str(evaluation.get("metric_id") or "dfm")
        check_id = str(evaluation.get("check_id") or "")
        issue_type_id, issue_type_label = classify_issue_type(check_id, metric_id)
        issues.append({
            "evaluation_id": str(evaluation.get("evaluation_id") or ""),
            "title": str(evaluation.get("rule_id") or "DFM rule")
            .replace("_", " ")
            .title(),
            "metric_id": metric_id,
            "check_id": check_id,
            "issue_type_id": issue_type_id,
            "issue_type_label": issue_type_label,
            "actual": evaluation.get("actual"),
            "expected": evaluation.get("expected"),
            "operator": str(evaluation.get("operator") or ""),
            "measurement_ids": measurement_ids,
            "geometry_refs": refs,
            "triangle_refs": triangle_refs,
        })

    features: list[dict[str, Any]] = []
    for item in features_payload.get("features", []):
        if not isinstance(item, dict) or not item.get("feature_id"):
            continue
        refs = [
            ref
            for ref in item.get("geometry_refs", [])
            if isinstance(ref, dict)
            and ref.get("kind") in {"face", "edge", "solid", "vertex"}
            and isinstance(ref.get("index"), int)
            and ref["index"] > 0
        ]
        features.append({
            "feature_id": str(item["feature_id"]),
            "kind": str(item.get("kind") or "feature"),
            "subtype": str(item.get("subtype") or ""),
            "confidence": item.get("confidence"),
            "geometry_refs": refs,
            "parameters": (
                item["parameters"] if isinstance(item.get("parameters"), dict) else {}
            ),
            "method": str(item.get("method") or ""),
            "diagnostics": (
                item["diagnostics"] if isinstance(item.get("diagnostics"), dict) else {}
            ),
        })

    output_dir = project_dir / "runs" / run_id / "artifacts"
    output_path = output_dir / "dfm_viewer.json"
    payload = {
        "schema_version": 2,
        "contract_version": "hermes.dfm.viewer/v2",
        "status": "completed",
        "run_id": run_id,
        "input_sha256": measurements_payload.get("input_sha256"),
        "process": plan.process,
        "scope_id": plan.scope_id,
        "scope_version": plan.scope_version,
        "verification_level": (
            "experimental" if "occt_cpp" in plan.analyzer_keys else "reference"
        ),
        "scene_path": Path(by_kind["render_scene"].relative_path).name,
        "topology_path": Path(by_kind["topology_map"].relative_path).name,
        "measurements_path": Path(by_kind["measurements"].relative_path).name,
        "issue_count": len(issues),
        "issue_type_counts": summarize_issue_types(issues),
        "issues": issues,
        "feature_count": len(features),
        "features": features,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ArtifactRecord(
        f"artifact_{run_id}_dfm_viewer",
        "dfm_viewer",
        output_path.relative_to(project_dir).as_posix(),
        "application/vnd.hermes.dfm-viewer+json",
        _utc_now(),
    )


def materialize_preview_manifest(
    project_dir: Path,
    run_id: str,
    input_sha256: str,
    artifacts: list[ArtifactRecord],
) -> ArtifactRecord | None:
    """Create the same viewer contract immediately after STEP registration."""

    by_kind = {item.kind: item for item in artifacts}
    if not {"render_scene", "topology_map"}.issubset(by_kind):
        return None

    output_dir = project_dir / "runs" / run_id / "artifacts"
    output_path = output_dir / "dfm_viewer.json"
    payload = {
        "schema_version": 2,
        "contract_version": "hermes.dfm.viewer/v2",
        "status": "preview",
        "run_id": run_id,
        "input_sha256": input_sha256,
        "process": "injection",
        "scope_id": "injection.geometry-core",
        "scope_version": "4.0.0",
        "verification_level": "experimental",
        "scene_path": Path(by_kind["render_scene"].relative_path).name,
        "topology_path": Path(by_kind["topology_map"].relative_path).name,
        "measurements_path": None,
        "issue_count": 0,
        "issue_type_counts": [],
        "issues": [],
        "feature_count": 0,
        "features": [],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ArtifactRecord(
        f"artifact_{run_id}_dfm_viewer",
        "dfm_viewer",
        output_path.relative_to(project_dir).as_posix(),
        "application/vnd.hermes.dfm-viewer+json",
        _utc_now(),
    )
