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
    import vtk

    pull_dir = legacy.unit(args.pull_dir)
    summary: dict[str, legacy.Any] = {
        "enabled": True,
        "negative_draft_face_count": 0,
        "side_action_candidate_count": 0,
        "hole_draft_undercut_count": 0,
        "hole_draft_pocket_candidate_count": 0,
        "hole_draft_neutral_or_ok_count": 0,
        "pull_dir": pull_dir,
    }
    negative_threshold = -legacy.math.sin(
        legacy.math.radians(args.undercut_negative_draft_deg)
    )
    info_by_index = legacy.face_info_by_index(face_infos)
    adjacent_by_face: dict[int, set[int]] = {
        face.face_index: set() for face in face_infos
    }
    for first_index, second_index, _edge_index in legacy.adjacent_face_pairs(
        shape, occ, face_infos
    ):
        adjacent_by_face.setdefault(first_index, set()).add(second_index)
        adjacent_by_face.setdefault(second_index, set()).add(first_index)
    bbox = legacy.shape_bbox(shape, occ)
    ray_length = legacy.bounds_diag(bbox) * 2.5
    min_hit_distance = max(
        args.model_tolerance_mm * 8.0, legacy.bounds_diag(bbox) * 0.0005
    )
    polydata = legacy.triangulate_shape(shape, occ, args.mesh_deflection_mm)
    tree = vtk.vtkOBBTree()
    tree.SetDataSet(polydata)
    tree.BuildLocator()
    shape_faces = [
        occ.topods.Face(raw_face)
        for raw_face in legacy.iter_shapes(shape, occ.TopAbs_FACE, occ)
    ]
    boundary_metrics_by_face = {
        index: legacy.face_boundary_metrics(face_shape, occ)
        for (index, face_shape) in enumerate(shape_faces, start=1)
    }
    hole_undercut_wall_faces: set[int] = set()
    for face in face_infos:
        if face.normal is None:
            continue
        pull_projection = legacy.dot(face.normal, pull_dir)
        signed_angle = legacy.math.degrees(
            legacy.math.asin(max(-1.0, min(1.0, pull_projection)))
        )
        adjacent_infos = [
            info_by_index[index]
            for index in adjacent_by_face.get(face.face_index, set())
            if index in info_by_index
        ]
        pocket = legacy.analyze_hole_pocket_draft(
            face,
            adjacent_infos,
            boundary_metrics_by_face.get(face.face_index),
            pull_dir,
            tree,
            ray_length,
            min_hit_distance,
            args,
        )
        if pocket is not None:
            summary["hole_draft_pocket_candidate_count"] += 1
            bad_walls = pocket["bad_walls"]
            if not bad_walls:
                summary["hole_draft_neutral_or_ok_count"] += 1
                continue
            worst_wall = min(bad_walls, key=lambda item: float(item["wall_projection"]))
            release_dir = tuple((float(value) for value in pocket["release_dir"]))
            hole_undercut_wall_faces.update((int(item["face"]) for item in bad_walls))
            summary["hole_draft_undercut_count"] += 1
            legacy.make_issue(
                issues,
                counters,
                "hole_draft_undercut",
                "孔拔模后倒扣风险",
                "high",
                f"孔/凹位底面 {face.face_index} 沿可释放方向 {legacy.format_vec(release_dir)} 检查时，邻接孔壁存在约 {float(worst_wall['reverse_draft_deg']):.2f}° 的反向拔模，形成倒扣风险。",
                face.center,
                {
                    "face": face.face_index,
                    "release_dir": release_dir,
                    "pull_dir": pull_dir,
                    "cap_pull_projection": pull_projection,
                    "opening_first_hit_mm": pocket["opening_first_hit_mm"],
                    "opposite_first_hit_mm": pocket["opposite_first_hit_mm"],
                    "cap_boundary": pocket["cap_boundary"],
                    "pocket_wall_faces": pocket["pocket_wall_faces"],
                    "bad_wall_faces": [item["face"] for item in bad_walls],
                    "wall_draft_checks": bad_walls,
                    "worst_reverse_draft_deg": worst_wall["reverse_draft_deg"],
                },
                args.max_issues_per_code,
                refs=[{"kind": "face", "index": face.face_index}]
                + [
                    {"kind": "face", "index": int(item["face"])}
                    for item in bad_walls[:2]
                ],
                view_dir=release_dir,
            )
            continue
        if (
            face.kind == "cylinder"
            and face.axis is not None
            and (face.area >= args.undercut_min_area_mm2)
            and legacy.is_hole_like_cylinder(face, args.max_side_action_diameter_mm)
        ):
            axis_pull_alignment = abs(legacy.dot(legacy.unit(face.axis), pull_dir))
            side_action_surface = (
                abs(pull_projection) <= args.side_action_surface_pull_abs_dot_max
            )
            if (
                axis_pull_alignment < args.side_core_axis_pull_abs_dot_max
                and side_action_surface
            ):
                slider_dir = legacy.unit(face.axis)
                anchor = legacy.cylinder_axis_center(face)
                summary["side_action_candidate_count"] += 1
                legacy.make_issue(
                    issues,
                    counters,
                    "side_action_cylinder",
                    "侧向抽芯/滑块候选",
                    "medium",
                    f"圆柱特征轴线与主拔模方向近似垂直，轴线夹角指标 {axis_pull_alignment:.3f}，可能需要侧向抽芯或滑块方向确认。",
                    anchor,
                    {
                        "face": face.face_index,
                        "radius_mm": face.radius,
                        "axis_pull_alignment": axis_pull_alignment,
                        "pull_dir": pull_dir,
                        "candidate_slider_dir": slider_dir,
                    },
                    args.max_issues_per_code,
                    refs=[{"kind": "face", "index": face.face_index}],
                    view_dir=slider_dir,
                )
                continue
        (release_dir, release_hit, opposite_hit) = (
            legacy.choose_release_direction_by_visibility(
                tree, face.center, pull_dir, ray_length, min_hit_distance
            )
        )
        if face.face_index in hole_undercut_wall_faces:
            continue
        release_projection = legacy.dot(face.normal, release_dir)
        release_signed_angle = legacy.math.degrees(
            legacy.math.asin(max(-1.0, min(1.0, release_projection)))
        )
        side_face = abs(release_projection) < args.undercut_side_face_abs_dot_max
        if (
            side_face
            and release_projection < negative_threshold
            and (face.area >= args.undercut_min_area_mm2)
            and (face.kind != "cylinder")
        ):
            slider_dir = legacy.unit(
                legacy.sub(
                    face.normal,
                    legacy.mul(release_dir, legacy.dot(face.normal, release_dir)),
                ),
                face.normal,
            )
            summary["negative_draft_face_count"] += 1
            legacy.make_issue(
                issues,
                counters,
                "undercut_negative_draft",
                "倒扣/负拔模风险",
                "high",
                f"面 {face.face_index} 按局部可释放方向 {legacy.format_vec(release_dir)} 检查时存在约 {release_signed_angle:.2f}° 的负拔模趋势，可能需要改拔模或侧向机构。",
                face.center,
                {
                    "face": face.face_index,
                    "signed_draft_deg": release_signed_angle,
                    "pull_projection": pull_projection,
                    "release_projection": release_projection,
                    "pull_dir": pull_dir,
                    "release_dir": release_dir,
                    "release_first_hit_mm": release_hit,
                    "opposite_first_hit_mm": opposite_hit,
                    "candidate_slider_dir": slider_dir,
                },
                args.max_issues_per_code,
                refs=[{"kind": "face", "index": face.face_index}],
                view_dir=slider_dir,
            )
    return summary
