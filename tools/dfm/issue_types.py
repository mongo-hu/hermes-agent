"""Stable issue-type classification shared by DFM report consumers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


_CHECK_LABELS_ZH = {
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


def classify_issue_type(check_id: object, metric_id: object) -> tuple[str, str]:
    """Return the business Check identity and a compact display label.

    A failed Check is the issue. The metric is only a compatibility fallback for
    older runs that predate the explicit Check identity.
    """

    check = str(check_id or "").strip()
    metric = str(metric_id or "").strip()
    issue_type_id = check or metric or "check.unknown"
    searchable = f"{check} {metric}".lower()

    if label := _CHECK_LABELS_ZH.get(issue_type_id.upper()):
        return issue_type_id, label

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
