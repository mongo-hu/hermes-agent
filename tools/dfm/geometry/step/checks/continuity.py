from __future__ import annotations

from .. import legacy_analyzer as legacy


def run(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    face_infos: list[legacy.FaceInfo],
    issues: list[legacy.Issue],
    counters: dict[str, int],
    args: legacy.argparse.Namespace,
) -> dict[str, legacy.Any]:
    info_by_index = legacy.face_info_by_index(face_infos)
    pairs = legacy.adjacent_face_pairs(shape, occ, face_infos)
    summary: dict[str, legacy.Any] = {
        "enabled": True,
        "adjacent_pair_count": len(pairs),
        "skipped_plane_plane_pair_count": 0,
        "skipped_plane_cylinder_pair_count": 0,
        "skipped_plane_other_pair_count": 0,
        "skipped_cylinder_other_pair_count": 0,
        "skipped_hard_edge_pair_count": 0,
        "g1_break_count": 0,
        "g2_jump_count": 0,
    }
    for first_index, second_index, edge_index in pairs:
        first = info_by_index.get(first_index)
        second = info_by_index.get(second_index)
        if (
            first is None
            or second is None
            or first.normal is None
            or (second.normal is None)
        ):
            continue
        (relevant, skipped_reason) = legacy.continuity_pair_is_relevant(
            first,
            second,
            args.continuity_include_plane_plane,
            args.continuity_include_plane_cylinder,
            args.continuity_include_plane_other,
            args.continuity_include_cylinder_other,
        )
        if not relevant:
            if skipped_reason == "plane_cylinder":
                summary["skipped_plane_cylinder_pair_count"] += 1
            elif skipped_reason == "plane_other":
                summary["skipped_plane_other_pair_count"] += 1
            elif skipped_reason == "cylinder_other":
                summary["skipped_cylinder_other_pair_count"] += 1
            else:
                summary["skipped_plane_plane_pair_count"] += 1
            continue
        angle = legacy.safe_acos_deg(legacy.dot(first.normal, second.normal))
        if angle > 90.0:
            angle = 180.0 - angle
        if angle > args.continuity_max_smooth_break_angle_deg and (
            not args.continuity_include_hard_edges
        ):
            summary["skipped_hard_edge_pair_count"] += 1
            continue
        anchor = legacy.mul(legacy.add(first.center, second.center), 0.5)
        refs = [
            {"kind": "face", "index": first.face_index},
            {"kind": "face", "index": second.face_index},
        ]
        view = legacy.unit(legacy.add(first.normal, second.normal), first.normal)
        if angle > args.continuity_g1_angle_deg:
            summary["g1_break_count"] += 1
            legacy.make_issue(
                issues,
                counters,
                "surface_g1_break",
                "曲面/面片切向不连续",
                "medium" if angle > args.continuity_g1_high_angle_deg else "low",
                f"相邻面法向夹角约 {angle:.2f}°，超过 G1 连续性阈值 {args.continuity_g1_angle_deg:.2f}°。",
                anchor,
                {
                    "face_a": first.face_index,
                    "face_b": second.face_index,
                    "shared_edge_map_index": edge_index,
                    "normal_angle_deg": angle,
                    "threshold_deg": args.continuity_g1_angle_deg,
                },
                args.max_issues_per_code,
                refs=refs,
                view_dir=view,
            )
            continue
        if first.curvature is not None and second.curvature is not None:
            curvature_jump = abs(first.curvature - second.curvature)
            if curvature_jump > args.continuity_g2_curvature_jump:
                summary["g2_jump_count"] += 1
                legacy.make_issue(
                    issues,
                    counters,
                    "surface_g2_jump",
                    "曲率连续性突变",
                    "low",
                    f"相邻面曲率代理差约 {curvature_jump:.4f}，超过 G2 曲率跳变阈值 {args.continuity_g2_curvature_jump:.4f}。",
                    anchor,
                    {
                        "face_a": first.face_index,
                        "face_b": second.face_index,
                        "shared_edge_map_index": edge_index,
                        "curvature_jump": curvature_jump,
                        "threshold": args.continuity_g2_curvature_jump,
                    },
                    args.max_issues_per_code,
                    refs=refs,
                    view_dir=view,
                )
    return summary
