"""Physical STEP evidence renderers extracted during M1.2."""

from __future__ import annotations

from .. import legacy_analyzer as legacy


def render_issue_evidence(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    issues: list[legacy.Issue],
    out_dir: legacy.Path,
    args: legacy.argparse.Namespace,
) -> int:
    if args.no_render:
        legacy.emit_dfm_event("progress", stage="evidence_skipped", percent=78)
        return 0
    limit = args.max_evidence_issues
    selected = issues if limit is None else issues[:limit]
    bbox = legacy.shape_bbox(shape, occ)
    total = max(len(selected), 1)
    for index, issue in enumerate(selected, start=1):
        legacy.emit_dfm_event(
            "progress",
            stage="render_evidence",
            percent=64 + int((index - 1) / total * 14),
        )
        try:
            images = legacy.render_issue_with_vtk(
                shape,
                occ,
                issue,
                out_dir,
                args.image_width,
                args.image_height,
                args.mesh_deflection_mm,
                bbox,
            )
            issue.images = images
            issue.image = images[0] if images else None
        except Exception as exc:
            print(
                f"[warn] targeted render failed for {issue.id}: {exc}",
                file=legacy.sys.stderr,
            )
    legacy.emit_dfm_event("progress", stage="evidence_complete", percent=78)
    return len(selected)


def render_outputs(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    issues: list[legacy.Issue],
    out_dir: legacy.Path,
    args: legacy.argparse.Namespace,
) -> None:
    if args.no_render:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        screen_points = legacy.render_with_vtk(
            shape,
            occ,
            issues,
            out_dir,
            args.image_width,
            args.image_height,
            args.mesh_deflection_mm,
        )
        renderer_name = "VTK"
    except Exception as exc:
        print(
            f"[warn] VTK render failed, falling back to 2D projection: {exc}",
            file=legacy.sys.stderr,
        )
        screen_points = legacy.render_projection_pil(
            shape, occ, issues, out_dir, args.image_width, args.image_height
        )
        renderer_name = "PIL projection"
    base_path = out_dir / "model.png"
    overview_annotations = [
        (issue, screen_points[issue.id].point, screen_points[issue.id].radius)
        for issue in issues
        if issue.id in screen_points
    ]
    legacy.annotate_png(
        base_path,
        out_dir / "overview.png",
        overview_annotations,
        show_annotation_labels=False,
        show_annotation_ids=True,
    )


def export_highlighted_step(
    shape: legacy.Any,
    occ: legacy.SimpleNamespace,
    issues: list[legacy.Issue],
    out_path: legacy.Path,
) -> dict[str, legacy.Any]:
    doc = occ.TDocStd_Document("dfm-highlighted-step")
    shape_tool = occ.XCAFDoc_DocumentTool.ShapeTool(doc.Main())
    color_tool = occ.XCAFDoc_DocumentTool.ColorTool(doc.Main())
    base_color = occ.Quantity_Color(0.74, 0.72, 0.78, occ.Quantity_TOC_RGB)
    red = occ.Quantity_Color(1.0, 0.02, 0.02, occ.Quantity_TOC_RGB)
    base_label = shape_tool.AddShape(shape)
    color_tool.SetColor(base_label, base_color, occ.XCAFDoc_ColorGen)
    color_tool.SetColor(base_label, base_color, occ.XCAFDoc_ColorSurf)
    global_bbox = legacy.shape_bbox(shape, occ)
    global_diag = legacy.bounds_diag(global_bbox)
    colored_refs: set[tuple[str, int]] = set()
    duplicate_highlights = 0
    preview_edges = 0

    def add_red_shape(highlight_shape: legacy.Any, color_type: legacy.Any) -> None:
        nonlocal duplicate_highlights
        label = shape_tool.AddShape(highlight_shape, False, False)
        color_tool.SetColor(label, red, occ.XCAFDoc_ColorGen)
        color_tool.SetColor(label, red, color_type)
        duplicate_highlights += 1

    for issue in issues:
        ref_shapes = legacy.ref_shapes_for_issue(shape, occ, issue)
        ref_bounds = [
            (ref, legacy.shape_bbox(ref_shape, occ)) for (ref, ref_shape) in ref_shapes
        ]
        annotation_point = legacy.issue_annotation_world_point(
            shape, occ, issue, ref_shapes
        )
        local_bbox = (
            legacy.union_bounds([bbox for (_ref, bbox) in ref_bounds]) or global_bbox
        )
        local_diag = max(legacy.bounds_diag(local_bbox), global_diag * 0.035)
        target = annotation_point or issue.anchor or legacy.bbox_center(local_bbox)
        evidence_bbox = legacy.issue_evidence_bbox(
            issue, ref_bounds, global_diag, target, local_diag
        )
        for ref, ref_shape in ref_shapes:
            kind = str(ref.get("kind", ""))
            index = int(ref.get("index", 0))
            if index <= 0 or (kind, index) in colored_refs:
                continue
            color_type = (
                occ.XCAFDoc_ColorSurf if kind == "face" else occ.XCAFDoc_ColorCurv
            )
            if not color_tool.SetColor(ref_shape, red, color_type):
                add_red_shape(ref_shape, color_type)
            colored_refs.add((kind, index))
        for preview_edge in legacy.issue_preview_edges(
            occ, issue, evidence_bbox, global_diag, target, local_diag
        ):
            add_red_shape(preview_edge, occ.XCAFDoc_ColorCurv)
            preview_edges += 1
    writer = occ.STEPCAFControl_Writer()
    writer.SetColorMode(True)
    with legacy.suppress_native_output():
        written = writer.Perform(doc, str(out_path))
    if not written:
        raise RuntimeError(f"OpenCascade failed to write highlighted STEP: {out_path}")
    legacy.emit_artifact_event(out_path, "step")
    return {
        "file": out_path.name,
        "path": str(out_path),
        "colored_ref_count": len(colored_refs),
        "duplicate_highlight_count": duplicate_highlights,
        "preview_edge_count": preview_edges,
    }
