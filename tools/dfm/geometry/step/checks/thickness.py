from __future__ import annotations

from .. import legacy_analyzer as legacy


def run(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    issues: list[legacy.Issue],
    counters: dict[str, int],
    args: legacy.argparse.Namespace,
) -> dict[str, legacy.Any]:
    import vtk

    polydata = legacy.triangulate_shape(shape, occ, args.thickness_mesh_deflection_mm)
    summary: dict[str, legacy.Any] = {
        "enabled": True,
        "sample_count": 0,
        "valid_sample_count": 0,
        "min_thickness_mm": None,
        "max_thickness_mm": None,
        "avg_thickness_mm": None,
    }
    if polydata.GetNumberOfCells() <= 0:
        summary["warning"] = "No mesh cells available for thickness sampling."
        return summary
    tree = vtk.vtkOBBTree()
    tree.SetDataSet(polydata)
    tree.BuildLocator()
    bbox = legacy.shape_bbox(shape, occ)
    ray_length = legacy.bounds_diag(bbox) * 2.5
    min_hit_distance = max(args.thickness_min_hit_mm, args.model_tolerance_mm * 5.0)
    samples: list[dict[str, legacy.Any]] = []
    for center, normal, area in legacy.iter_triangle_samples(
        polydata, args.thickness_samples
    ):
        summary["sample_count"] += 1
        thickness = legacy.estimate_thickness_at_point(
            tree, center, normal, ray_length, min_hit_distance
        )
        if thickness is None or not legacy.math.isfinite(thickness):
            continue
        samples.append({
            "point": center,
            "normal": normal,
            "thickness_mm": thickness,
            "area_mm2": area,
        })
    summary["valid_sample_count"] = len(samples)
    if not samples:
        summary["warning"] = "No valid ray intersections found for thickness sampling."
        return summary
    samples.sort(key=lambda item: item["thickness_mm"])
    thickness_values = [float(item["thickness_mm"]) for item in samples]
    min_sample = samples[0]
    max_sample = samples[-1]
    summary.update({
        "min_thickness_mm": min(thickness_values),
        "max_thickness_mm": max(thickness_values),
        "avg_thickness_mm": sum(thickness_values) / len(thickness_values),
        "min_point": min_sample["point"],
        "max_point": max_sample["point"],
    })
    thin_samples = [
        sample for sample in samples if sample["thickness_mm"] < args.min_wall_mm
    ]
    cluster_distance = args.thickness_issue_cluster_mm
    if cluster_distance <= 0:
        cluster_distance = max(args.min_wall_mm * 3.0, 2.0)
    selected_thin_samples = legacy.spatially_distinct_samples(
        thin_samples, cluster_distance
    )
    summary["thin_wall_candidate_count"] = len(thin_samples)
    summary["thin_wall_report_count"] = len(selected_thin_samples)
    summary["thin_wall_cluster_mm"] = cluster_distance
    for sample in selected_thin_samples:
        thickness = float(sample["thickness_mm"])
        legacy.make_issue(
            issues,
            counters,
            "thin_wall_field",
            "厚度场薄壁风险",
            "high",
            f"厚度场采样检测到局部厚度约 {thickness:.3f} mm，低于壁厚阈值 {args.min_wall_mm:.3f} mm。",
            sample["point"],
            {
                "thickness_mm": thickness,
                "threshold_mm": args.min_wall_mm,
                "normal": sample["normal"],
            },
            args.max_issues_per_code,
            view_dir=sample["normal"],
        )
    if args.max_wall_mm > 0:
        thick_samples = [
            sample
            for sample in reversed(samples)
            if sample["thickness_mm"] > args.max_wall_mm
        ]
        for sample in thick_samples:
            thickness = float(sample["thickness_mm"])
            legacy.make_issue(
                issues,
                counters,
                "thick_section",
                "厚壁/缩水风险",
                "medium",
                f"厚度场采样检测到局部厚度约 {thickness:.3f} mm，高于厚壁阈值 {args.max_wall_mm:.3f} mm。",
                sample["point"],
                {
                    "thickness_mm": thickness,
                    "threshold_mm": args.max_wall_mm,
                    "normal": sample["normal"],
                },
                args.max_issues_per_code,
                view_dir=sample["normal"],
            )
    if (
        args.thickness_variation_ratio > 0
        and summary["min_thickness_mm"]
        and summary["max_thickness_mm"]
        and (summary["min_thickness_mm"] > args.model_tolerance_mm)
    ):
        ratio = summary["max_thickness_mm"] / summary["min_thickness_mm"]
        summary["variation_ratio"] = ratio
        nominal_limit = max(
            args.thickness_variation_nominal_max_mm, args.min_wall_mm * 3.0
        )
        nominal_wall_seen = (
            args.thickness_variation_nominal_max_mm <= 0
            or summary["min_thickness_mm"] <= nominal_limit
        )
        summary["variation_nominal_limit_mm"] = nominal_limit
        if (
            ratio > args.thickness_variation_ratio
            and nominal_wall_seen
            and (
                not selected_thin_samples
                or args.report_thickness_variation_with_thin_wall
            )
        ):
            legacy.make_issue(
                issues,
                counters,
                "thickness_variation",
                "壁厚变化过大",
                "medium",
                f"厚度场最大/最小厚度比约 {ratio:.2f}，超过阈值 {args.thickness_variation_ratio:.2f}。",
                min_sample["point"],
                {
                    "min_thickness_mm": summary["min_thickness_mm"],
                    "max_thickness_mm": summary["max_thickness_mm"],
                    "ratio": ratio,
                    "threshold_ratio": args.thickness_variation_ratio,
                    "normal": min_sample["normal"],
                },
                args.max_issues_per_code,
                view_dir=min_sample["normal"],
            )
        elif ratio > args.thickness_variation_ratio:
            if selected_thin_samples and (
                not args.report_thickness_variation_with_thin_wall
            ):
                summary["variation_suppressed_reason"] = (
                    "Thin-wall evidence already reports the controlling minimum-thickness region."
                )
            else:
                summary["variation_suppressed_reason"] = (
                    "Minimum sampled thickness is above the nominal-wall guard; use --thickness-variation-nominal-max-mm 0 or set --max-wall-mm for thick-section review."
                )
    return summary
