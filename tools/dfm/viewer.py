"""Materialize a small desktop contract for the interactive OCCT DFM viewer."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .contracts import ArtifactRecord, PlanRecord
from .errors import DFMError


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
        refs = []
        seen = set()
        for measurement in linked:
            for ref in measurement.get("geometry_refs", []):
                if not isinstance(ref, dict):
                    continue
                key = (str(ref.get("kind") or ""), ref.get("index"))
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
        issues.append({
            "evaluation_id": str(evaluation.get("evaluation_id") or ""),
            "title": str(evaluation.get("rule_id") or "DFM rule")
            .replace("_", " ")
            .title(),
            "metric_id": str(evaluation.get("metric_id") or "dfm"),
            "actual": evaluation.get("actual"),
            "expected": evaluation.get("expected"),
            "operator": str(evaluation.get("operator") or ""),
            "measurement_ids": measurement_ids,
            "geometry_refs": refs,
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
            "experimental" if "occt" in plan.analyzer_keys else "reference"
        ),
        "scene_path": Path(by_kind["render_scene"].relative_path).name,
        "topology_path": Path(by_kind["topology_map"].relative_path).name,
        "measurements_path": Path(by_kind["measurements"].relative_path).name,
        "issue_count": len(issues),
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
