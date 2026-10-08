"""Adapt existing DFM run artifacts to the HTML runtime JSONL contract."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from ...contracts import ArtifactRecord, InputRecord, PlanRecord
from ...errors import DFMError


_PROCESS_LABELS = {
    "injection": "注塑 (Injection)",
    "die_casting": "压铸 (Die Casting)",
}

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_object(project_dir: Path, artifact: ArtifactRecord) -> dict[str, Any]:
    path = (project_dir / artifact.relative_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DFMError(
            "report_input_invalid",
            f"The {artifact.kind} artifact cannot be used by the HTML report.",
            {"path": artifact.relative_path},
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            "report_input_invalid",
            f"The {artifact.kind} artifact must contain an object.",
            {"path": artifact.relative_path},
        )
    return payload


def _read_objects(project_dir: Path, artifact: ArtifactRecord) -> list[dict[str, Any]]:
    path = _artifact_path(project_dir, artifact)
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DFMError(
            "report_input_invalid",
            f"The {artifact.kind} artifact cannot be used by the HTML report.",
            {"path": artifact.relative_path},
        ) from exc
    if any(not isinstance(row, dict) for row in rows):
        raise DFMError(
            "report_input_invalid",
            f"The {artifact.kind} artifact must contain JSON objects.",
            {"path": artifact.relative_path},
        )
    return rows


def _artifact_path(project_dir: Path, artifact: ArtifactRecord) -> Path:
    path = (project_dir / artifact.relative_path).resolve()
    if not path.is_relative_to(project_dir.resolve()) or not path.is_file():
        raise DFMError(
            "report_input_invalid",
            "An HTML report resource is outside the DFM project or missing.",
            {"path": artifact.relative_path},
        )
    return path


def _relative(path: Path, base_dir: Path) -> str:
    return Path(os.path.relpath(path.resolve(), base_dir.resolve())).as_posix()


def _one(
    by_kind: dict[str, list[ArtifactRecord]], kind: str
) -> ArtifactRecord | None:
    values = by_kind.get(kind, [])
    return values[-1] if values else None


def _html_field_catalog(
    project_dir: Path,
    output_dir: Path,
    artifacts: list[ArtifactRecord],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    grouped: dict[
        tuple[str, str, str], list[tuple[ArtifactRecord, dict[str, Any]]]
    ] = {}
    for artifact in artifacts:
        payload = _read_object(project_dir, artifact)
        identity = (
            str(payload.get("metric_id") or ""),
            str(payload.get("quantity_id") or ""),
            str(payload.get("unit") or ""),
        )
        grouped.setdefault(identity, []).append((artifact, payload))

    catalog: list[dict[str, Any]] = []
    legacy: dict[str, str] = {}
    for index, (identity, sources) in enumerate(sorted(grouped.items()), start=1):
        metric_id, quantity_id, unit = identity
        if len(sources) == 1:
            path = _artifact_path(project_dir, sources[0][0])
        else:
            merged = {
                "schema_version": 2,
                "metric_id": metric_id,
                "quantity_id": quantity_id,
                "unit": unit,
                "samples": [sample for _, payload in sources for sample in payload.get("samples", [])],
                "cells": [cell for _, payload in sources for cell in payload.get("cells", [])],
                "source_artifact_ids": [artifact.artifact_id for artifact, _ in sources],
            }
            path = output_dir / f"report_scalar_field_{index:03d}.json"
            path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        entry = {
            "id": f"field_{index:03d}",
            "metric_id": metric_id,
            "quantity_id": quantity_id,
            "unit": unit,
            "path": _relative(path, output_dir),
            "source_count": len(sources),
        }
        catalog.append(entry)
        if metric_id == "injection.geometry.wall_thickness":
            legacy["thickness"] = entry["path"]
        elif metric_id == "injection.geometry.draft":
            legacy["draft"] = entry["path"]
    return catalog, legacy


def materialize_html_runtime(
    project_dir: Path,
    run_id: str,
    plan: PlanRecord,
    inputs: list[InputRecord],
    artifacts: list[ArtifactRecord],
    semantic_artifacts: list[ArtifactRecord] | None = None,
    observation_refs: set[str] | None = None,
) -> ArtifactRecord | None:
    """Write runtime_data.jsonl from existing artifacts without recomputation.

    Use every scalar field produced by the current geometry run. The report
    may have any number of fields; a PDF drawing is optional.
    """

    by_kind: dict[str, list[ArtifactRecord]] = {}
    for artifact in artifacts:
        by_kind.setdefault(artifact.kind, []).append(artifact)

    report_artifact = _one(by_kind, "report_json")
    scene_artifact = _one(by_kind, "render_scene")
    geometry_artifact = _one(by_kind, "evidence_geometry")
    active_ids = set(plan.input_ids)
    drawings = [
        item
        for item in inputs
        if item.input_id in active_ids
        and item.kind == "drawing"
        and Path(item.relative_path).suffix.lower() == ".pdf"
    ]
    if (
        report_artifact is None
        or scene_artifact is None
    ):
        return None
    if len(drawings) > 1:
        return None

    drawing = drawings[0] if drawings else None
    observation_artifact = None
    drawing_observations: list[dict[str, object]] = []
    drawing_semantics: dict[str, object] = {"observations": drawing_observations}
    if drawing is not None:
        observation_artifact = next(
            (
                item
                for item in reversed(semantic_artifacts or [])
                if item.kind == "drawing_observations"
                and item.logical_id.startswith(
                    f"drawing-observations:{drawing.input_id}:"
                )
            ),
            None,
        )
        if observation_artifact is None:
            return None
        drawing_observations = [
            row
            for row in _read_objects(project_dir, observation_artifact)
            if row.get("input_id") == drawing.input_id
            and (
                observation_refs is None
                or row.get("observation_id") in observation_refs
            )
        ]
        drawing_semantics = {
            "input_id": drawing.input_id,
            "input_sha256": drawing.sha256,
            "source_artifact_id": observation_artifact.artifact_id,
            "observations": drawing_observations,
        }

    report = _read_object(project_dir, report_artifact)
    if str(report.get("run_id") or "") != run_id:
        raise DFMError(
            "report_input_invalid",
            "The JSON report belongs to a different DFM run.",
        )

    output_dir = project_dir / "runs" / run_id / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    if geometry_artifact is None:
        geometry_path = output_dir / "report_evidence_geometry.json"
        geometry_path.write_text('{"failed_patches": []}', encoding="utf-8")
    else:
        geometry_path = _artifact_path(project_dir, geometry_artifact)
    field_catalog, legacy_fields = _html_field_catalog(
        project_dir, output_dir, by_kind.get("scalar_field", [])
    )
    rule_snapshot_path = output_dir / "dfm_rule_snapshot.json"
    rule_snapshot = {
        "schema_version": 1,
        "snapshot_id": plan.ontology_snapshot_id,
        "content_sha256": plan.ontology_snapshot_sha256,
        "process": plan.process,
        "scope_id": plan.scope_id,
        "scope_version": plan.scope_version,
        "rules": {key: value.to_dict() for key, value in plan.rules.items()},
        "rule_bindings": [item.to_dict() for item in plan.rule_bindings],
    }
    rule_snapshot_path.write_text(
        json.dumps(rule_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
    model_metrics = [
        {"label": "测量特征总数", "value": stats.get("measurement_count", 0)},
        {"label": "评估特征总数", "value": stats.get("evaluation_count", 0)},
        {"label": "未通过项总数", "value": stats.get("failed_count", 0)},
        {"label": "无法判定项总数", "value": stats.get("indeterminate_count", 0)},
    ]
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    global_issue_metadata = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("images") or issue.get("image"):
            continue
        issue_id = str(issue.get("id") or "")
        if issue_id:
            global_issue_metadata.append(
                {
                    "issue_id": issue_id,
                    "source_issue_id": issue_id,
                }
            )

    runtime_path = output_dir / "runtime_data.jsonl"
    drawing_path = None
    if drawing is not None:
        drawing_path = (project_dir / drawing.relative_path).resolve()
        if (
            not drawing_path.is_relative_to(project_dir.resolve())
            or not drawing_path.is_file()
        ):
            raise DFMError(
                "report_input_invalid",
                "The drawing input referenced by the HTML report is missing.",
            )
    resources = {
        "evidence_root": ".",
        "scene_path": _relative(
            _artifact_path(project_dir, scene_artifact), output_dir
        ),
        "scalar_fields": legacy_fields,
        "field_catalog": field_catalog,
        "evidence_geometry_path": _relative(geometry_path, output_dir),
        "rule_library_path": _relative(rule_snapshot_path, output_dir),
    }
    if drawing_path is not None and observation_artifact is not None:
        resources.update(
            {
                "drawing_pdf_path": _relative(drawing_path, output_dir),
                "drawing_observations_path": _relative(
                    _artifact_path(project_dir, observation_artifact), output_dir
                ),
            }
        )
    runtime = {
        "schema_version": "dfm-html-runtime/v1",
        "display": {
            "process": _PROCESS_LABELS.get(plan.process, plan.process),
            "rule_scope": f"{plan.scope_id}@{plan.scope_version}",
        },
        "report": report,
        "drawing_semantics": drawing_semantics,
        "global_issue_metadata": global_issue_metadata,
        "model_metrics": model_metrics,
        "resources": resources,
    }
    runtime_path.write_text(
        json.dumps(runtime, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return ArtifactRecord(
        artifact_id=f"artifact_{run_id}_report_html_runtime",
        kind="report_html_runtime",
        relative_path=runtime_path.relative_to(project_dir).as_posix(),
        media_type="application/x-ndjson",
        created_at=_utc_now(),
    )
