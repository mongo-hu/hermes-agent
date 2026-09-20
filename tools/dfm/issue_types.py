"""Stable issue-type classification shared by DFM report consumers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def classify_issue_type(check_id: object, metric_id: object) -> tuple[str, str]:
    """Return the business Check identity and a compact display label.

    A failed Check is the issue. The metric is only a compatibility fallback for
    older runs that predate the explicit Check identity.
    """

    check = str(check_id or "").strip()
    metric = str(metric_id or "").strip()
    issue_type_id = check or metric or "check.unknown"
    searchable = f"{check} {metric}".lower()

    if "boss" in searchable and ("wall" in searchable or "thickness" in searchable):
        label = "螺钉柱柱壁问题"
    elif "rib" in searchable and "thickness" in searchable:
        label = "加强筋厚度问题"
    elif "draft" in searchable:
        label = "拔模角问题"
    elif "wall_thickness" in searchable or "minimum_thickness" in searchable:
        label = "壁厚问题"
    elif "undercut" in searchable:
        label = "倒扣问题"
    elif "radius" in searchable or "fillet" in searchable:
        label = "圆角半径问题"
    else:
        label = issue_type_id

    return issue_type_id, label


def summarize_issue_types(issues: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Count failed issues by business Check without consulting severity."""

    counts: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_type_id, label = classify_issue_type(
            issue.get("check_id") or issue.get("issue_type_id"),
            issue.get("metric_id") or issue.get("code"),
        )
        if issue_type_id not in counts:
            counts[issue_type_id] = {
                "issue_type_id": issue_type_id,
                "label": label,
                "count": 0,
            }
        counts[issue_type_id]["count"] += 1
    return list(counts.values())
