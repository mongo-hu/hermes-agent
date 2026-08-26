"""Face quality checks extracted from the M1 compatibility analyzer."""

from __future__ import annotations


def run(face_infos, issues, counters, args):
    from .. import legacy_analyzer as legacy

    summary: dict[str, Any] = {
        "enabled": True,
        "small_face_count": 0,
        "sliver_face_count": 0,
    }
    for face in face_infos:
        if face.area <= args.model_tolerance_mm * args.model_tolerance_mm:
            continue

        reported_small = False
        if args.min_face_area_mm2 > 0 and face.area < args.min_face_area_mm2:
            summary["small_face_count"] += 1
            reported_small = True
            legacy.make_issue(
                issues,
                counters,
                "small_face",
                "碎小面/微小面",
                "low",
                f"检测到面积约 {face.area:.4f} mm² 的微小面，低于阈值 {args.min_face_area_mm2:.4f} mm²，可能是导出碎面或不可制造微特征。",
                face.center,
                {
                    "face": face.face_index,
                    "area_mm2": face.area,
                    "threshold_mm2": args.min_face_area_mm2,
                },
                args.max_issues_per_code,
                refs=[{"kind": "face", "index": face.face_index}],
                view_dir=face.normal,
            )

        long_span, width, aspect = legacy.face_long_span_and_width(face)
        if (
            not reported_small
            and args.max_sliver_face_width_mm > 0
            and args.sliver_face_aspect_ratio > 0
            and long_span >= args.min_sliver_face_length_mm
            and width < args.max_sliver_face_width_mm
            and aspect > args.sliver_face_aspect_ratio
        ):
            summary["sliver_face_count"] += 1
            legacy.make_issue(
                issues,
                counters,
                "sliver_face",
                "狭长碎面/窄面",
                "low",
                f"检测到狭长面，估算宽度约 {width:.4f} mm、长宽比约 {aspect:.1f}，可能导致加工/网格/模具细节风险。",
                face.center,
                {
                    "face": face.face_index,
                    "estimated_width_mm": width,
                    "long_span_mm": long_span,
                    "aspect_ratio": aspect,
                    "width_threshold_mm": args.max_sliver_face_width_mm,
                    "aspect_threshold": args.sliver_face_aspect_ratio,
                },
                args.max_issues_per_code,
                refs=[{"kind": "face", "index": face.face_index}],
                view_dir=face.normal,
            )
    return summary
