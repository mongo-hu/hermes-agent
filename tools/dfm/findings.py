"""Materialize findings from backend-neutral evaluations and evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import ArtifactRecord, FindingRecord
from .errors import DFMError


def materialize_evaluated_findings(
    project_dir: Path, artifacts: list[ArtifactRecord]
) -> list[FindingRecord]:
    """Create findings from explicit evaluations and evidence records."""

    by_kind = {item.kind: item for item in artifacts}
    measurements_artifact = by_kind.get("measurements")
    evaluations_artifact = by_kind.get("evaluations")
    if measurements_artifact is None or evaluations_artifact is None:
        return []
    measurements_payload = _read_object(
        project_dir, measurements_artifact, "measurements_invalid"
    )
    evaluations_payload = _read_object(
        project_dir, evaluations_artifact, "evaluations_invalid"
    )
    evidence_payload = (
        _read_object(project_dir, by_kind["evidence_records"], "evidence_invalid")
        if "evidence_records" in by_kind
        else {"records": []}
    )
    measurements = {
        str(item.get("measurement_id")): item
        for item in measurements_payload.get("measurements", [])
        if isinstance(item, dict) and item.get("measurement_id")
    }
    evidence_by_evaluation: dict[str, list[str]] = {}
    for record in evidence_payload.get("records", []):
        if not isinstance(record, dict) or not record.get("evidence_id"):
            continue
        for evaluation_id in record.get("evaluation_ids", []):
            evidence_by_evaluation.setdefault(str(evaluation_id), []).append(
                str(record["evidence_id"])
            )

    input_hash = str(measurements_payload.get("input_sha256") or "")
    results = []
    for evaluation in evaluations_payload.get("evaluations", []):
        if not isinstance(evaluation, dict) or evaluation.get("outcome") != "fail":
            continue
        evaluation_id = str(evaluation.get("evaluation_id") or "")
        measurement_ids = [
            str(item) for item in evaluation.get("measurement_ids", [])
        ]
        linked = [measurements[item] for item in measurement_ids if item in measurements]
        rule_id = str(evaluation.get("rule_id") or "unmapped")
        rule_version = str(evaluation.get("rule_version") or "1")
        stable = hashlib.sha256(
            f"{input_hash}:{evaluation_id}".encode("utf-8")
        ).hexdigest()[:20]
        results.append(
            FindingRecord(
                finding_id=f"finding_{stable}",
                title=rule_id.replace(".", " ").replace("_", " ").title(),
                severity="unclassified",
                status="open",
                evaluation_ids=[evaluation_id],
                measurement_ids=measurement_ids,
                metric_ids=[str(evaluation.get("metric_id") or "unmapped")],
                region_refs=sorted(
                    {
                        str(ref)
                        for item in linked
                        for ref in item.get("region_refs", [])
                    }
                ),
                evidence_refs=sorted(
                    evidence_by_evaluation.get(evaluation_id, [])
                ),
                rule_refs=[
                    f"{rule_id}@{rule_version}",
                    f"sha256:{evaluation.get('rule_hash')}",
                ],
                recommendation=(
                    "Review and correct the geometry identified by the evaluated rule."
                ),
                feature_refs=sorted(
                    {
                        str(ref)
                        for item in linked
                        for ref in item.get("feature_refs", [])
                    }
                    | {str(ref) for ref in evaluation.get("feature_refs", [])}
                ),
                check_ids=(
                    [str(evaluation["check_id"])]
                    if evaluation.get("check_id")
                    else []
                ),
            )
        )
    return results


def _read_object(
    project_dir: Path, artifact: ArtifactRecord, error_code: str
) -> dict:
    try:
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DFMError(
            error_code, f"The {artifact.kind} artifact could not be read."
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            error_code, f"The {artifact.kind} artifact has an invalid contract."
        )
    return payload
