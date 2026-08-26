from __future__ import annotations

from .. import legacy_analyzer as legacy


def run(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    planes: list[legacy.PlaneFace],
    issues: list[legacy.Issue],
    counters: dict[str, int],
    args: legacy.argparse.Namespace,
) -> None:
    if not planes:
        return
    max_pair_distance = max(
        args.min_wall_mm,
        args.max_planar_step_mm,
        args.min_slot_width_mm,
        args.max_local_boss_span_mm,
    )
    if max_pair_distance <= 0:
        return
    plane_by_index = {plane.face_index: plane for plane in planes}
    adjacent_by_face: dict[int, set[int]] = {
        plane.face_index: set() for plane in planes
    }
    for first_index, second_index, _edge_index in legacy.adjacent_face_pairs(
        shape, occ, []
    ):
        adjacent_by_face.setdefault(first_index, set()).add(second_index)
        adjacent_by_face.setdefault(second_index, set()).add(first_index)
    wall_probe = (
        legacy.build_wall_thickness_probe(shape, occ, args)
        if args.local_boss_thickness_ratio > 0
        else None
    )
    for index, first in enumerate(planes):
        for second in planes[index + 1 :]:
            parallel_dot = legacy.dot(first.normal, second.normal)
            parallel_limit = legacy.math.cos(
                legacy.math.radians(args.parallel_tolerance_deg)
            )
            if abs(parallel_dot) < parallel_limit:
                continue
            same_direction = parallel_dot >= parallel_limit
            opposite_direction = parallel_dot <= -parallel_limit
            normal = first.normal
            signed_distance = abs(
                legacy.dot(normal, second.point) - legacy.dot(normal, first.point)
            )
            if signed_distance <= args.model_tolerance_mm:
                continue
            if signed_distance > max_pair_distance:
                continue
            dominant_axis = max(range(3), key=lambda axis: abs(normal[axis]))
            plane_axes = [axis for axis in range(3) if axis != dominant_axis]
            if not legacy.bbox_overlap_on_axes(
                first.bbox, second.bbox, plane_axes, args.model_tolerance_mm
            ):
                continue
            overlap_lengths = legacy.bbox_overlap_lengths_on_axes(
                first.bbox, second.bbox, plane_axes, args.model_tolerance_mm
            )
            anchor = legacy.mul(legacy.add(first.center, second.center), 0.5)
            pair_metric = {
                "face_a": first.face_index,
                "face_b": second.face_index,
                "distance_mm": signed_distance,
                "pull_dir": args.pull_dir,
            }
            refs = [
                {"kind": "face", "index": first.face_index},
                {"kind": "face", "index": second.face_index},
            ]
            planar_step_parallel_limit = legacy.math.cos(
                legacy.math.radians(args.planar_step_parallel_tolerance_deg)
            )
            planar_step_candidate = (
                bool(overlap_lengths)
                and min(overlap_lengths) >= args.min_planar_step_overlap_mm
                and (parallel_dot >= planar_step_parallel_limit)
            )
            machining_gap_reported = False
            if (
                opposite_direction
                and args.min_machining_gap_mm > 0
                and (signed_distance < args.min_machining_gap_mm)
                and overlap_lengths
                and (min(overlap_lengths) >= args.min_machining_gap_overlap_mm)
            ):
                machining_gap_reported = True
                legacy.make_issue(
                    issues,
                    counters,
                    "narrow_machining_gap",
                    "方孔/加工间隙过小",
                    "high",
                    f"两处相对平面净距约 {signed_distance:.3f} mm，低于加工间隙阈值 {args.min_machining_gap_mm:.3f} mm，方孔/窄缝可能无法加工或无法保证强度。",
                    anchor,
                    {
                        **pair_metric,
                        "threshold_mm": args.min_machining_gap_mm,
                        "overlap_lengths_mm": overlap_lengths,
                    },
                    args.max_issues_per_code,
                    refs=refs,
                    view_dir=normal,
                )
            if (
                not machining_gap_reported
                and opposite_direction
                and (0 < args.min_wall_mm)
                and (signed_distance < args.min_wall_mm)
            ):
                legacy.make_issue(
                    issues,
                    counters,
                    "thin_wall",
                    "薄壁风险",
                    "high",
                    f"两处相对/平行平面间距约 {signed_distance:.3f} mm，低于壁厚阈值 {args.min_wall_mm:.3f} mm。",
                    anchor,
                    {**pair_metric, "threshold_mm": args.min_wall_mm},
                    args.max_issues_per_code,
                    refs=refs,
                    view_dir=normal,
                )
            elif (
                not machining_gap_reported
                and opposite_direction
                and (0 < args.min_slot_width_mm)
                and (signed_distance < args.min_slot_width_mm)
            ):
                legacy.make_issue(
                    issues,
                    counters,
                    "narrow_slot",
                    "窄槽加工风险",
                    "medium",
                    f"两处相对/平行平面间距约 {signed_distance:.3f} mm，低于槽宽阈值 {args.min_slot_width_mm:.3f} mm。",
                    anchor,
                    {**pair_metric, "threshold_mm": args.min_slot_width_mm},
                    args.max_issues_per_code,
                    refs=refs,
                    view_dir=normal,
                )
            elif (
                not machining_gap_reported
                and same_direction
                and planar_step_candidate
                and (0 < args.max_planar_step_mm)
                and (signed_distance <= args.max_planar_step_mm)
            ):
                legacy.make_issue(
                    issues,
                    counters,
                    "planar_step",
                    "A面与基准面疑似未对齐",
                    "medium",
                    f"检测到相邻/重叠平面存在约 {signed_distance:.3f} mm 的小台阶或错位，需确认 A 面与基板/基准面是否对齐。",
                    anchor,
                    {**pair_metric, "threshold_mm": args.max_planar_step_mm},
                    args.max_issues_per_code,
                    refs=refs,
                    view_dir=normal,
                )
            elif (
                opposite_direction
                and args.local_boss_thickness_ratio > 0
                and (signed_distance <= args.max_local_boss_span_mm)
                and (min(first.area, second.area) >= args.min_local_boss_side_area_mm2)
                and overlap_lengths
                and (min(overlap_lengths) >= args.min_local_boss_overlap_mm)
            ):
                if wall_probe is None:
                    continue
                cap_face = legacy.local_boss_cap_face(
                    first,
                    second,
                    plane_by_index,
                    adjacent_by_face,
                    pull_dir=args.pull_dir,
                )
                if cap_face is None or cap_face.normal is None:
                    continue
                thickness_hit = legacy.thickness_hit_from_surface(
                    wall_probe["tree"],
                    cap_face.center,
                    cap_face.normal,
                    float(wall_probe["ray_length"]),
                    float(wall_probe["min_hit_distance"]),
                )
                if thickness_hit is None:
                    continue
                local_thickness = float(thickness_hit["thickness_mm"])
                nominal_thickness = float(wall_probe["nominal_thickness_mm"])
                thickness_ratio = (
                    local_thickness / nominal_thickness
                    if nominal_thickness > 1e-09
                    else float("inf")
                )
                if thickness_ratio <= args.local_boss_thickness_ratio:
                    continue
                boss_refs = [
                    {"kind": "face", "index": cap_face.face_index},
                    {"kind": "face", "index": first.face_index},
                    {"kind": "face", "index": second.face_index},
                ]
                legacy.make_issue(
                    issues,
                    counters,
                    "local_boss_thick",
                    "局部凸块/筋位过厚",
                    "medium",
                    f"局部凸块沿法向厚度约 {local_thickness:.3f} mm，名义壁厚约 {nominal_thickness:.3f} mm，厚度比约 {thickness_ratio:.2f}，可能造成壁厚差异、缩水或成型风险。",
                    cap_face.center,
                    {
                        "face_a": first.face_index,
                        "face_b": second.face_index,
                        "side_span_mm": signed_distance,
                        "pull_dir": args.pull_dir,
                        "distance_mm": local_thickness,
                        "local_thickness_mm": local_thickness,
                        "nominal_thickness_mm": nominal_thickness,
                        "thickness_ratio": thickness_ratio,
                        "threshold_ratio": args.local_boss_thickness_ratio,
                        "cap_face": cap_face.face_index,
                        "cap_area_mm2": cap_face.area,
                        "measurement_start": thickness_hit["start"],
                        "measurement_end": thickness_hit["end"],
                        "overlap_lengths_mm": overlap_lengths,
                        "measurement_dir": thickness_hit["direction"],
                        "nominal_thickness_p25_mm": wall_probe.get("p25_thickness_mm"),
                        "nominal_thickness_p75_mm": wall_probe.get("p75_thickness_mm"),
                        "nominal_sample_count": wall_probe.get("sample_count"),
                    },
                    args.max_issues_per_code,
                    refs=boss_refs,
                    view_dir=cap_face.normal,
                )
