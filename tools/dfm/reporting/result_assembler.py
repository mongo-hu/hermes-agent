"""Assemble backend-neutral evaluations and evidence into final reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from ..contracts import ArtifactRecord, PlanRecord
from ..errors import DFMError
from . import render_default_reports
from .pptx import pptx_available


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def materialize_result_reports(
    project_dir: Path,
    run_id: str,
    plan: PlanRecord,
    artifacts: list[ArtifactRecord],
) -> list[ArtifactRecord]:
    """Create one final result shape for every compliant objective backend."""

    by_kind = {item.kind: item for item in artifacts}
    if "measurements" not in by_kind or "evaluations" not in by_kind:
        return []
    measurements_payload = _read(project_dir, by_kind["measurements"])
    evaluations_payload = _read(project_dir, by_kind["evaluations"])
    evidence_payload = (
        _read(project_dir, by_kind["evidence_records"])
        if "evidence_records" in by_kind
        else {"records": []}
    )
    measurements = {
        str(item.get("measurement_id")): item
        for item in measurements_payload.get("measurements", [])
        if isinstance(item, dict) and item.get("measurement_id")
    }
    artifact_by_id = {item.artifact_id: item for item in artifacts}
    evidence_by_evaluation: dict[str, list[dict[str, Any]]] = {}
    for record in evidence_payload.get("records", []):
        if not isinstance(record, dict):
            continue
        image = artifact_by_id.get(str(record.get("artifact_ref") or ""))
        if image is None or image.kind != "evidence_image":
            continue
        for evaluation_id in record.get("evaluation_ids", []):
            evidence_by_evaluation.setdefault(str(evaluation_id), []).append(
                {**record, "image": Path(image.relative_path).name}
            )

    issues: list[dict[str, Any]] = []
    for evaluation in evaluations_payload.get("evaluations", []):
        if not isinstance(evaluation, dict) or evaluation.get("outcome") != "fail":
            continue
        evaluation_id = str(evaluation.get("evaluation_id") or "")
        linked = [
            measurements[item]
            for item in evaluation.get("measurement_ids", [])
            if item in measurements
        ]
        images = [
            item["image"] for item in evidence_by_evaluation.get(evaluation_id, [])
        ]
        quality = linked[0].get("quality", {}) if linked else {}
        feature_refs = sorted(
            {
                str(ref)
                for item in linked
                for ref in item.get("feature_refs", [])
            }
            | {str(ref) for ref in evaluation.get("feature_refs", [])}
        )
        region_refs = sorted(
            {
                str(ref)
                for item in linked
                for ref in item.get("region_refs", [])
            }
            | {str(ref) for ref in evaluation.get("region_refs", [])}
        )
        issues.append(
            {
                "id": evaluation_id,
                "code": str(evaluation.get("metric_id") or "dfm"),
                "title": str(evaluation.get("rule_id") or "DFM rule").replace(
                    "_", " "
                ).title(),
                "severity": "unclassified",
                "message": (
                    f"Actual {evaluation.get('actual')} does not satisfy "
                    f"{evaluation.get('operator')} {evaluation.get('expected')}."
                ),
                "metric": {
                    "actual": evaluation.get("actual"),
                    "expected": evaluation.get("expected"),
                    "operator": evaluation.get("operator"),
                    "rule_id": evaluation.get("rule_id"),
                    "rule_version": evaluation.get("rule_version"),
                    "rule_hash": evaluation.get("rule_hash"),
                    "measurement_ids": evaluation.get("measurement_ids", []),
                    "backend": quality.get("backend"),
                    "certified": quality.get("certified"),
                    "algorithm_version": linked[0].get("algorithm_version")
                    if linked
                    else None,
                },
                "images": images,
                "image": images[0] if images else None,
                "feature_refs": feature_refs,
                "region_refs": region_refs,
                "recommendation": "Correct the highlighted geometry and rerun the same plan.",
            }
        )

    output_dir = project_dir / "runs" / run_id / "artifacts"
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "input_sha256": measurements_payload.get("input_sha256"),
        "process": plan.process,
        "scope_id": plan.scope_id,
        "scope_version": plan.scope_version,
        "producer": "hermes-result-assembler-v1",
        "producer_contract": "evaluated_objective_result",
        "stats": {
            "measurement_count": len(measurements),
            "evaluation_count": len(evaluations_payload.get("evaluations", [])),
            "failed_count": len(issues),
        },
        "issues": issues,
    }
    json_path = output_dir / "dfm_report.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = output_dir / "dfm_report.md"
    lines = [
        "# DFM Report",
        "",
        f"- Process: {plan.process}",
        f"- Scope: {plan.scope_id}@{plan.scope_version}",
        f"- Failed checks: {len(issues)}",
        "",
    ]
    for issue in issues:
        lines.extend(
            [
                f"## {issue['title']}",
                "",
                issue["message"],
                "",
                f"- Rule: {issue['metric']['rule_id']}@{issue['metric']['rule_version']}",
                f"- Evidence: {', '.join(issue['images']) or 'none'}",
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    generated = [
        ArtifactRecord(
            f"artifact_{run_id}_report_json",
            "report_json",
            json_path.relative_to(project_dir).as_posix(),
            "application/json",
            _utc_now(),
        ),
        ArtifactRecord(
            f"artifact_{run_id}_report_markdown",
            "report_markdown",
            markdown_path.relative_to(project_dir).as_posix(),
            "text/markdown",
            _utc_now(),
        ),
    ]
    if pptx_available():
        generated.extend(
            ArtifactRecord(
                f"artifact_{run_id}_{report.kind}",
                report.kind,
                report.path.relative_to(project_dir).as_posix(),
                report.media_type,
                _utc_now(),
            )
            for report in render_default_reports(
                artifact_dir=output_dir,
                result=result,
                process=plan.process,
                scope_id=plan.scope_id,
            )
        )
    return generated


def _read(project_dir: Path, artifact: ArtifactRecord) -> dict[str, Any]:
    try:
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DFMError(
            "report_input_invalid", f"The {artifact.kind} artifact cannot be read."
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            "report_input_invalid", f"The {artifact.kind} artifact must be an object."
        )
    return payload
