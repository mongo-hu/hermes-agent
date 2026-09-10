"""Small-feature and planar-draft checks over the shared STEP index."""

from __future__ import annotations

import math


def run(model, occ, args, issues, counters) -> None:
    from .. import legacy_analyzer as legacy

    selected = set(getattr(args, "operation", None) or [])
    small_enabled = not selected or "inspect_small_features" in selected
    draft_enabled = not selected or "measure_draft" in selected
    pull_dir = legacy.unit(args.pull_dir)

    for entry in model.faces:
        face = entry.shape
        if entry.surface_type == occ.GeomAbs_Cylinder and small_enabled:
            cylinder = entry.surface.Cylinder()
            axis = legacy.dir_to_tuple(cylinder.Axis().Direction())
            radius = float(cylinder.Radius())
            if radius <= args.model_tolerance_mm:
                continue
            diameter = radius * 2.0
            if diameter < args.min_hole_diameter_mm:
                legacy.make_issue(
                    issues,
                    counters,
                    "small_cylindrical_feature",
                    "孔径/圆柱特征过小",
                    "medium",
                    f"检测到直径约 {diameter:.3f} mm 的圆柱/孔类特征，低于阈值 {args.min_hole_diameter_mm:.3f} mm。",
                    entry.center,
                    {
                        "face": entry.index,
                        "diameter_mm": diameter,
                        "threshold_mm": args.min_hole_diameter_mm,
                    },
                    args.max_issues_per_code,
                    refs=[{"kind": "face", "index": entry.index}],
                    view_dir=axis,
                )
            if radius < args.min_tool_radius_mm:
                legacy.make_issue(
                    issues,
                    counters,
                    "small_tool_radius",
                    "刀具半径风险",
                    "medium",
                    f"检测到半径约 {radius:.3f} mm 的圆柱/圆角特征，低于刀具半径阈值 {args.min_tool_radius_mm:.3f} mm。",
                    entry.center,
                    {
                        "face": entry.index,
                        "radius_mm": radius,
                        "threshold_mm": args.min_tool_radius_mm,
                    },
                    args.max_issues_per_code,
                    refs=[{"kind": "face", "index": entry.index}],
                    view_dir=axis,
                )
        elif entry.surface_type == occ.GeomAbs_Plane and draft_enabled:
            plane = entry.surface.Plane()
            normal = legacy.dir_to_tuple(plane.Axis().Direction())
            if face.Orientation() == occ.TopAbs_REVERSED:
                normal = legacy.mul(normal, -1.0)
            angle = math.degrees(math.asin(min(1.0, abs(legacy.dot(normal, pull_dir)))))
            if (
                args.min_draft_deg > 0
                and angle < 45.0
                and entry.area >= args.min_draft_area_mm2
                and angle + args.angle_tolerance_deg < args.min_draft_deg
            ):
                legacy.make_issue(
                    issues,
                    counters,
                    "low_draft",
                    "拔模角不足",
                    "medium",
                    f"平面侧壁相对拔模方向 {pull_dir} 的拔模角约 {angle:.2f}°，低于阈值 {args.min_draft_deg:.2f}°。",
                    entry.center,
                    {
                        "face": entry.index,
                        "draft_angle_deg": angle,
                        "threshold_deg": args.min_draft_deg,
                        "area_mm2": entry.area,
                        "pull_dir": pull_dir,
                    },
                    args.max_issues_per_code,
                    refs=[{"kind": "face", "index": entry.index}],
                    view_dir=normal,
                )

    if not small_enabled:
        return
    seen_circles: set[tuple[float, float, float, float]] = set()
    tiny_centers: list[tuple[float, float, float]] = []
    for entry in model.edges:
        if 0.0 < entry.length < args.min_edge_mm:
            cluster = max(args.min_edge_mm * 2.0, args.model_tolerance_mm * 10.0)
            if not any(
                legacy.distance(entry.center, existing) <= cluster
                for existing in tiny_centers
            ):
                tiny_centers.append(entry.center)
                legacy.make_issue(
                    issues,
                    counters,
                    "tiny_edge",
                    "细碎短边",
                    "low",
                    f"检测到长度约 {entry.length:.4f} mm 的短边，低于阈值 {args.min_edge_mm:.4f} mm，可能是导出碎面或制造微小特征。",
                    entry.center,
                    {
                        "edge": entry.index,
                        "length_mm": entry.length,
                        "threshold_mm": args.min_edge_mm,
                    },
                    args.max_issues_per_code,
                    refs=[{"kind": "edge", "index": entry.index}],
                )
        if entry.curve_type != occ.GeomAbs_Circle:
            continue
        circle = entry.curve.Circle()
        radius = float(circle.Radius())
        if radius <= args.model_tolerance_mm:
            continue
        center = legacy.gp_to_tuple(circle.Location())
        key = (
            round(center[0], 3),
            round(center[1], 3),
            round(center[2], 3),
            round(radius, 3),
        )
        if key in seen_circles:
            continue
        seen_circles.add(key)
        diameter = radius * 2.0
        if diameter < args.min_hole_diameter_mm:
            axis = legacy.dir_to_tuple(circle.Axis().Direction())
            legacy.make_issue(
                issues,
                counters,
                "small_circular_edge",
                "圆形边/孔径过小",
                "medium",
                f"检测到直径约 {diameter:.3f} mm 的圆形边，低于阈值 {args.min_hole_diameter_mm:.3f} mm。",
                center,
                {
                    "edge": entry.index,
                    "diameter_mm": diameter,
                    "threshold_mm": args.min_hole_diameter_mm,
                },
                args.max_issues_per_code,
                refs=[{"kind": "edge", "index": entry.index}],
                view_dir=axis,
            )
