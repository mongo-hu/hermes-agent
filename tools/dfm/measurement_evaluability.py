"""Keep unresolved geometric evidence separate from inapplicable checks."""

from typing import Any

REVIEW_REASONS = frozenset({
    "REASON_INCOMPLETE_COVERAGE", "REASON_UNRESOLVED_VARIABLE_FACE",
    "REASON_UNRESOLVED_ROOT_RADIUS", "REASON_UNRESOLVED_CORNER_SUPPORT",
    "REASON_NONCONVERGENT_TRANSITION", "REASON_PATH_NOT_CONVERGED",
})


def requires_manual_review(quality: Any) -> bool:
    if not isinstance(quality, dict):
        return False
    evaluability = quality.get("evaluability") or {}
    coverage = quality.get("thickness_field_coverage") or {}
    unresolved = coverage.get("unresolved_variable_face_indices", coverage.get("variable_face_indices", []))
    return bool(
        evaluability.get("state") == "indeterminate"
        or evaluability.get("requires_manual_review") is True
        or REVIEW_REASONS.intersection(evaluability.get("reason_codes") or [])
        or coverage.get("coverage_complete") is False
        or (coverage.get("measurement_scope") == "uniform_wall" and unresolved)
    )


def manual_review_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "result_id": row.get("result_id", row.get("measurement_id")),
        "feature_id": row.get("feature_id"),
        "geometric_id": row.get("geometric_id", row.get("quantity_id")),
        "reason": row.get("reason") or "无法判定，需人工复核。",
        "reason_codes": (row.get("quality", {}).get("evaluability") or {}).get("reason_codes", []),
        "geometry_refs": row.get("geometry_refs", (row.get("evidence") or {}).get("geometry_refs", [])),
    } for row in rows if requires_manual_review(row.get("quality"))]
