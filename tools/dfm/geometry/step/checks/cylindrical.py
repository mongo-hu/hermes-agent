from __future__ import annotations

from .. import legacy_analyzer as legacy


def run(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    face_infos: list[legacy.FaceInfo],
    global_bbox: legacy.BBox,
    issues: list[legacy.Issue],
    counters: dict[str, int],
    args: legacy.argparse.Namespace,
) -> dict[str, legacy.Any]:
    shape_faces = [
        occ.topods.Face(raw_face)
        for raw_face in legacy.iter_shapes(shape, occ.TopAbs_FACE, occ)
    ]
    shape_edges = [
        occ.topods.Edge(raw_edge)
        for raw_edge in legacy.iter_shapes(shape, occ.TopAbs_EDGE, occ)
    ]
    edge_face_map = occ.TopTools_IndexedDataMapOfShapeListOfShape()
    occ.topexp.MapShapesAndAncestors(
        shape, occ.TopAbs_EDGE, occ.TopAbs_FACE, edge_face_map
    )
    info_by_index = legacy.face_info_by_index(face_infos)
    cylinders = legacy.cylinder_faces(face_infos)
    mouth_clearances_by_face = {
        face.face_index: legacy.cylinder_mouth_edge_clearances(
            face, shape_faces, shape_edges, edge_face_map, info_by_index, occ, args
        )
        for face in cylinders
    }
    hole_like_cylinders = [
        face for face in cylinders if mouth_clearances_by_face.get(face.face_index)
    ]
    summary: dict[str, legacy.Any] = {
        "enabled": True,
        "cylinder_count": len(cylinders),
        "hole_like_cylinder_count": len(hole_like_cylinders),
        "hole_edge_clearance_count": 0,
        "hole_web_count": 0,
        "deep_hole_count": 0,
    }
    for face in hole_like_cylinders:
        assert face.axis is not None and face.radius is not None
        diameter = face.radius * 2.0
        if args.min_hole_edge_distance_mm > 0:
            mouth_clearances = mouth_clearances_by_face.get(face.face_index, [])
            if mouth_clearances:
                nearest = min(
                    mouth_clearances, key=lambda item: float(item["clearance_mm"])
                )
                clearance = float(nearest["clearance_mm"])
                if clearance < args.min_hole_edge_distance_mm:
                    hole_point = tuple(
                        (float(value) for value in nearest["hole_point"])
                    )
                    outer_point = tuple(
                        (float(value) for value in nearest["outer_point"])
                    )
                    anchor = legacy.mul(legacy.add(hole_point, outer_point), 0.5)
                    clearance_direction = legacy.unit(
                        legacy.sub(outer_point, hole_point),
                        legacy.perpendicular_vector(face.axis),
                    )
                    refs = [{"kind": "face", "index": face.face_index}]
                    if nearest.get("hole_edge") is not None:
                        refs.append({
                            "kind": "edge",
                            "index": int(nearest["hole_edge"]),
                        })
                    if nearest.get("outer_edge") is not None:
                        refs.append({
                            "kind": "edge",
                            "index": int(nearest["outer_edge"]),
                        })
                    summary["hole_edge_clearance_count"] += 1
                    legacy.make_issue(
                        issues,
                        counters,
                        "hole_edge_clearance",
                        "孔到外边距离不足",
                        "medium" if clearance > args.model_tolerance_mm else "high",
                        f"圆柱/孔特征到外轮廓最近距离约 {clearance:.3f} mm，低于阈值 {args.min_hole_edge_distance_mm:.3f} mm。",
                        anchor,
                        {
                            "face": face.face_index,
                            "clearance_mm": clearance,
                            "threshold_mm": args.min_hole_edge_distance_mm,
                            "diameter_mm": diameter,
                            "mouth_face": nearest.get("mouth_face"),
                            "hole_edge": nearest.get("hole_edge"),
                            "outer_edge": nearest.get("outer_edge"),
                            "hole_point": hole_point,
                            "outer_point": outer_point,
                            "clearance_direction": clearance_direction,
                        },
                        args.max_issues_per_code,
                        refs=refs,
                        view_dir=face.axis,
                    )
        if args.max_hole_depth_ratio > 0 and diameter > args.model_tolerance_mm:
            depth = legacy.bbox_extent_along_dir(face.bbox, face.axis)
            ratio = depth / diameter
            diameter_ok = (
                args.deep_hole_max_diameter_mm <= 0
                or diameter <= args.deep_hole_max_diameter_mm
            )
            if diameter_ok and ratio > args.max_hole_depth_ratio:
                summary["deep_hole_count"] += 1
                legacy.make_issue(
                    issues,
                    counters,
                    "deep_hole_ratio",
                    "深孔/长细圆柱风险",
                    "medium",
                    f"圆柱/孔特征深度约 {depth:.3f} mm、直径约 {diameter:.3f} mm，长径比约 {ratio:.2f}，超过阈值 {args.max_hole_depth_ratio:.2f}。",
                    face.center,
                    {
                        "face": face.face_index,
                        "depth_mm": depth,
                        "diameter_mm": diameter,
                        "depth_diameter_ratio": ratio,
                        "threshold_ratio": args.max_hole_depth_ratio,
                        "axis": face.axis,
                    },
                    args.max_issues_per_code,
                    refs=[{"kind": "face", "index": face.face_index}],
                    view_dir=legacy.perpendicular_vector(face.axis),
                )
    if args.min_hole_web_mm > 0:
        for first_index, first in enumerate(hole_like_cylinders):
            for second in hole_like_cylinders[first_index + 1 :]:
                gap_info = legacy.cylinder_gap(first, second)
                if gap_info is None:
                    continue
                (gap, direction) = gap_info
                if -args.model_tolerance_mm <= gap < args.min_hole_web_mm:
                    assert first.radius is not None and second.radius is not None
                    anchor = legacy.add(
                        first.center,
                        legacy.mul(direction, first.radius + max(gap, 0.0) / 2.0),
                    )
                    summary["hole_web_count"] += 1
                    legacy.make_issue(
                        issues,
                        counters,
                        "hole_web_thin",
                        "孔间薄桥/间距不足",
                        "medium" if gap > args.model_tolerance_mm else "high",
                        f"相邻圆柱/孔特征之间净距约 {gap:.3f} mm，低于阈值 {args.min_hole_web_mm:.3f} mm。",
                        anchor,
                        {
                            "face_a": first.face_index,
                            "face_b": second.face_index,
                            "web_mm": gap,
                            "threshold_mm": args.min_hole_web_mm,
                            "radius_a_mm": first.radius,
                            "radius_b_mm": second.radius,
                            "direction": direction,
                        },
                        args.max_issues_per_code,
                        refs=[
                            {"kind": "face", "index": first.face_index},
                            {"kind": "face", "index": second.face_index},
                        ],
                        view_dir=first.axis,
                    )
    return summary
