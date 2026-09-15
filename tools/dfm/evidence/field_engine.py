"""Turn evaluated scalar fields into precisely linked evidence images."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import operator
from pathlib import Path
from typing import Any, Callable

from ..contracts import ArtifactRecord, EvidenceRecord, GeometryRef
from ..errors import DFMError
from ..geometry.snapshot_hash import render_mesh_content_sha256


EVIDENCE_SCHEMA_VERSION = 2
_VIEWS_PER_PATCH = 3


def _between(value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        lower, upper = expected.get("lower"), expected.get("upper")
    elif isinstance(expected, (list, tuple)) and len(expected) == 2:
        lower, upper = expected
    else:
        return False
    return bool(lower <= value <= upper)


_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
    "between": _between,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FieldEvidenceEngine:
    """Render precise evidence from any backend's objective scalar fields."""

    version = "hermes-field-evidence-v5"

    def materialize(
        self,
        project_dir: Path,
        run_id: str,
        artifacts: list[ArtifactRecord],
        *,
        max_images: int = 12,
    ) -> list[ArtifactRecord]:
        by_kind = {item.kind: item for item in artifacts}
        measurements_artifact = by_kind.get("measurements")
        evaluations_artifact = by_kind.get("evaluations")
        if measurements_artifact is None or evaluations_artifact is None:
            return []

        measurements_payload = _read_json(project_dir, measurements_artifact)
        evaluations_payload = _read_json(project_dir, evaluations_artifact)
        input_sha256 = str(measurements_payload.get("input_sha256") or "")
        if evaluations_payload.get("run_id") != run_id:
            raise DFMError(
                "evidence_input_invalid",
                "The evaluation artifact belongs to a different run.",
            )
        measurements = {
            str(item.get("measurement_id")): item
            for item in measurements_payload.get("measurements", [])
            if isinstance(item, dict) and item.get("measurement_id")
        }
        artifact_by_id = {item.artifact_id: item for item in artifacts}
        pull_direction = _field_pull_direction(project_dir, artifacts)
        patches: list[dict[str, Any]] = []
        for evaluation in evaluations_payload.get("evaluations", []):
            if not isinstance(evaluation, dict) or evaluation.get("outcome") != "fail":
                continue
            # A scalar field can only be thresholded with the direct rule that
            # produced it. Composite expressions (for example boss/main-wall
            # thickness ratios) need a dedicated renderer for their numerator
            # and denominator regions; applying the derived threshold to each
            # raw field sample would create false evidence.
            expression = evaluation.get("expression")
            if expression is not None and not (
                isinstance(expression, dict)
                and set(expression) == {"operand"}
                and isinstance(expression.get("operand"), str)
                and expression["operand"] in (evaluation.get("operand_values") or {})
            ):
                continue
            comparison = _OPERATORS.get(str(evaluation.get("operator") or ""))
            if comparison is None:
                raise DFMError(
                    "evidence_rule_invalid",
                    "Evidence geometry cannot apply the evaluation operator.",
                )
            linked = [
                measurements[item]
                for item in evaluation.get("measurement_ids", [])
                if item in measurements
            ]
            for measurement in linked:
                for field_ref in measurement.get("field_refs", []):
                    field_artifact = artifact_by_id.get(str(field_ref))
                    if field_artifact is None or field_artifact.kind != "scalar_field":
                        raise DFMError(
                            "evidence_field_missing",
                            "A failed measurement references a missing scalar field.",
                            {"field_ref": field_ref},
                        )
                    field = _read_json(project_dir, field_artifact)
                    self._validate_field_identity(
                        project_dir,
                        field,
                        run_id,
                        input_sha256,
                        measurement,
                        artifact_by_id,
                    )
                    scene = _read_json(
                        project_dir,
                        artifact_by_id[str(field.get("scene_ref") or "")],
                    )
                    patches.extend(
                        self._failed_patches(
                            evaluation,
                            measurement,
                            field_ref=str(field_ref),
                            field=field,
                            scene=scene,
                            comparison=comparison,
                        )
                    )

        output_dir = project_dir / "runs" / run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        geometry_path = output_dir / "evidence_geometry.json"
        geometry_path.write_text(
            json.dumps(
                {
                    "schema_version": EVIDENCE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "input_sha256": input_sha256,
                    "producer": "hermes-evidence-engine",
                    "producer_version": self.version,
                    "failed_patches": patches,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        generated = [
            ArtifactRecord(
                f"artifact_{run_id}_evidence_geometry",
                "evidence_geometry",
                geometry_path.relative_to(project_dir).as_posix(),
                "application/json",
                _utc_now(),
            )
        ]

        records: list[EvidenceRecord] = []
        evaluation_by_id = {
            str(item.get("evaluation_id")): item
            for item in evaluations_payload.get("evaluations", [])
            if isinstance(item, dict)
        }
        image_index = 0
        image_limit = max(0, max_images)
        patch_limit = math.ceil(image_limit / _VIEWS_PER_PATCH)
        selected_patches = _select_representative_patches(patches, patch_limit)
        for patch in selected_patches:
            scene_artifact = artifact_by_id[patch["scene_ref"]]
            scene = _read_json(project_dir, scene_artifact)
            evaluation = evaluation_by_id[patch["evaluation_id"]]
            for view in _adaptive_views(scene, patch, pull_direction):
                if image_index >= image_limit:
                    break
                image_index += 1
                image_id = f"artifact_{run_id}_evidence_{image_index}"
                image_path = output_dir / f"evidence_{image_index:03d}.png"
                render_view = _presentation_view(scene, patch, view)
                self._render(scene, patch, image_path, render_view)
                image_artifact = ArtifactRecord(
                    image_id,
                    "evidence_image",
                    image_path.relative_to(project_dir).as_posix(),
                    "image/png",
                    _utc_now(),
                )
                generated.append(image_artifact)
                records.append(
                    EvidenceRecord(
                        evidence_id=f"evidence_{run_id}_{image_index}",
                        run_id=run_id,
                        input_sha256=input_sha256,
                        operation_id=str(evaluation.get("operation_id") or ""),
                        metric_id=str(evaluation.get("metric_id") or ""),
                        measurement_ids=[str(item) for item in patch["measurement_ids"]],
                        evaluation_ids=[patch["evaluation_id"]],
                        geometry_refs=[
                            GeometryRef.from_dict(item)
                            for item in patch["geometry_refs"]
                        ],
                        region_refs=[str(item) for item in patch["region_refs"]],
                        artifact_ref=image_id,
                        render={
                            "mode": "local_patch",
                            "producer": "hermes-evidence-renderer",
                            "version": self.version,
                            "viewport": [1440, 810],
                            "patch_id": patch["patch_id"],
                            "scene_ref": patch["scene_ref"],
                            "topology_snapshot_ref": patch["topology_snapshot_ref"],
                            "render_mesh_snapshot_ref": patch["render_mesh_snapshot_ref"],
                            "view_id": render_view["id"],
                            "view_label": render_view["label"],
                            "camera_direction": list(render_view["basis_d"]),
                            "camera_up": list(render_view["basis_v"]),
                            "camera_source": render_view["source"],
                            "presentation_mode": render_view.get("presentation_mode", "canonical"),
                        },
                        feature_refs=[str(item) for item in patch["feature_refs"]],
                    )
                )

        records_path = output_dir / "evidence_records.json"
        records_path.write_text(
            json.dumps(
                {
                    "schema_version": EVIDENCE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "input_sha256": input_sha256,
                    "producer": "hermes-evidence-renderer",
                    "producer_version": self.version,
                    "records": [item.to_dict() for item in records],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        generated.append(
            ArtifactRecord(
                f"artifact_{run_id}_evidence_records",
                "evidence_records",
                records_path.relative_to(project_dir).as_posix(),
                "application/json",
                _utc_now(),
            )
        )
        return generated

    @staticmethod
    def _validate_field_identity(
        project_dir: Path,
        field: dict[str, Any],
        run_id: str,
        input_sha256: str,
        measurement: dict[str, Any],
        artifact_by_id: dict[str, ArtifactRecord],
    ) -> None:
        expected = (
            run_id,
            input_sha256,
            measurement.get("operation_id"),
            measurement.get("metric_id"),
            measurement.get("quantity_id"),
        )
        actual = (
            field.get("run_id"),
            field.get("input_sha256"),
            field.get("operation_id"),
            field.get("metric_id"),
            field.get("quantity_id"),
        )
        if actual != expected:
            raise DFMError(
                "evidence_field_invalid",
                "The scalar field does not belong to its linked measurement.",
            )
        linked_payloads: dict[str, dict[str, Any]] = {}
        for ref, kind in (
            (field.get("scene_ref"), "render_scene"),
            (field.get("topology_map_ref"), "topology_map"),
        ):
            artifact = artifact_by_id.get(str(ref))
            if artifact is None or artifact.kind != kind:
                raise DFMError(
                    "evidence_field_invalid",
                    f"The scalar field references a missing {kind} artifact.",
                )
            linked = _read_json(project_dir, artifact)
            linked_payloads[kind] = linked
            if (
                linked.get("run_id") != run_id
                or linked.get("input_sha256") != input_sha256
            ):
                raise DFMError(
                    "evidence_field_invalid",
                    f"The linked {kind} artifact belongs to another run or input.",
                )
            if kind == "topology_map" and linked.get("scene_ref") != field.get(
                "scene_ref"
            ):
                raise DFMError(
                    "evidence_field_invalid",
                    "The topology map and scalar field reference different scenes.",
                )
        scene_triangles = {
            (
                str(primitive.get("render_mesh_snapshot_id")),
                str(primitive.get("primitive_id")),
                triangle_id,
            )
            for primitive in linked_payloads["render_scene"].get("primitives", [])
            for triangle_id, _triangle in enumerate(primitive.get("triangles", []))
        }
        mapped_triangles = {
            (
                str(ref.get("render_mesh_snapshot_id")),
                str(ref.get("primitive_id")),
                int(ref.get("triangle_id", -1)),
            )
            for face in linked_payloads["topology_map"].get("faces", [])
            for ref in face.get("triangle_refs", [])
        }
        field_triangles = {
            (
                str(cell.get("triangle_ref", {}).get("render_mesh_snapshot_id")),
                str(cell.get("triangle_ref", {}).get("primitive_id")),
                int(cell.get("triangle_ref", {}).get("triangle_id", -1)),
            )
            for cell in field.get("cells", [])
        }
        if not field_triangles.issubset(scene_triangles & mapped_triangles):
            raise DFMError(
                "evidence_field_invalid",
                "Scalar field cells are not present in both the scene and topology map.",
            )
        mapped_by_entity = {
            (
                str(face.get("geometry_ref", {}).get("topology_snapshot_id")),
                str(face.get("geometry_ref", {}).get("entity_id")),
                str(ref.get("render_mesh_snapshot_id")),
                str(ref.get("primitive_id")),
                int(ref.get("triangle_id", -1)),
            )
            for face in linked_payloads["topology_map"].get("faces", [])
            for ref in face.get("triangle_refs", [])
        }
        if any(
            (
                str(cell.get("geometry_ref", {}).get("topology_snapshot_id")),
                str(cell.get("geometry_ref", {}).get("entity_id")),
                str(cell.get("triangle_ref", {}).get("render_mesh_snapshot_id")),
                str(cell.get("triangle_ref", {}).get("primitive_id")),
                int(cell.get("triangle_ref", {}).get("triangle_id", -1)),
            )
            not in mapped_by_entity
            for cell in field.get("cells", [])
        ):
            raise DFMError(
                "evidence_field_invalid",
                "A scalar-field cell triangle is mapped to a different topology entity.",
            )
        scene_snapshot = linked_payloads["render_scene"].get("render_mesh_snapshot", {})
        topology_snapshot = linked_payloads["topology_map"].get("topology_snapshot", {})
        mesh_id = str(scene_snapshot.get("render_mesh_snapshot_id") or "")
        topology_id = str(topology_snapshot.get("topology_snapshot_id") or "")
        if (
            not mesh_id
            or not topology_id
            or field.get("render_mesh_snapshot_ref") != mesh_id
            or field.get("topology_snapshot_ref") != topology_id
            or linked_payloads["render_scene"].get("topology_snapshot_ref") != topology_id
            or linked_payloads["topology_map"].get("render_mesh_snapshot_ref") != mesh_id
            or scene_snapshot.get("topology_snapshot_id") != topology_id
            or scene_snapshot.get("input_sha256") != input_sha256
            or topology_snapshot.get("input_sha256") != input_sha256
        ):
            raise DFMError(
                "evidence_snapshot_mismatch",
                "Scalar field, topology, and render mesh do not share one immutable snapshot.",
            )
        primitives = linked_payloads["render_scene"].get("primitives", [])
        try:
            mesh_content_sha256 = render_mesh_content_sha256(primitives)
        except ValueError as exc:
            raise DFMError(
                "evidence_artifact_invalid",
                "The render mesh snapshot content is not canonicalizable.",
            ) from exc
        topology_payload = [
            {
                "entity_id": face.get("geometry_ref", {}).get("entity_id"),
                "kind": face.get("geometry_ref", {}).get("kind"),
                "index": face.get("geometry_ref", {}).get("index"),
            }
            for face in linked_payloads["topology_map"].get("faces", [])
        ]
        if (
            scene_snapshot.get("render_mesh_sha256") != mesh_content_sha256
            or scene_snapshot.get("triangle_count")
            != sum(len(item.get("triangles", [])) for item in primitives)
            or topology_snapshot.get("topology_content_sha256")
            != _stable_content_sha256(topology_payload)
        ):
            raise DFMError(
                "evidence_snapshot_mismatch",
                "The topology or render mesh content no longer matches its immutable snapshot.",
            )
        geometry_refs = [
            item.get("geometry_ref", {}) for item in field.get("samples", [])
        ] + [item.get("geometry_ref", {}) for item in field.get("cells", [])]
        if any(
            ref.get("topology_snapshot_id") != topology_id
            or ref.get("input_sha256") != input_sha256
            or not ref.get("entity_id")
            for ref in geometry_refs
        ):
            raise DFMError(
                "evidence_snapshot_mismatch",
                "Scalar field geometry refs do not belong to the linked topology snapshot.",
            )

    @staticmethod
    def _failed_patches(
        evaluation: dict[str, Any],
        measurement: dict[str, Any],
        *,
        field_ref: str,
        field: dict[str, Any],
        scene: dict[str, Any],
        comparison: Callable[[Any, Any], bool],
    ) -> list[dict[str, Any]]:
        expected = evaluation.get("expected")
        samples = {
            str(item.get("sample_id")): item
            for item in field.get("samples", [])
            if isinstance(item, dict) and item.get("sample_id")
        }
        failed_samples = {
            sample_id
            for sample_id, sample in samples.items()
            if not comparison(sample.get("value"), expected)
        }
        if not failed_samples:
            raise DFMError(
                "evidence_field_inconsistent",
                "A failed aggregate measurement has no failing scalar-field sample.",
                {"evaluation_id": evaluation.get("evaluation_id")},
            )
        failed_cells = [
            item
            for item in field.get("cells", [])
            if isinstance(item, dict)
            and failed_samples.intersection(str(value) for value in item.get("sample_ids", []))
        ]
        groups = _connected_cells(failed_cells, scene)
        if not groups and failed_samples:
            groups = [[]]

        results = []
        for index, cells in enumerate(groups, start=1):
            sample_ids = sorted({
                str(sample_id)
                for cell in cells
                for sample_id in cell.get("sample_ids", [])
                if str(sample_id) in failed_samples
            })
            if not sample_ids:
                sample_ids = sorted(failed_samples)
            points = [samples[item]["point"] for item in sample_ids]
            surface_normal = _average_direction(
                [samples[item].get("surface_normal") for item in sample_ids]
            )
            geometry_values = [
                samples[item]["geometry_ref"] for item in sample_ids
            ] + [cell["geometry_ref"] for cell in cells]
            geometry_refs = _unique_dicts(geometry_values)
            triangles = _unique_dicts([cell["triangle_ref"] for cell in cells])
            focus = min(
                (samples[item] for item in sample_ids),
                key=lambda item: float(item.get("value", 0)),
            )["point"]
            if str(evaluation.get("operator")) in {"<=", "<"}:
                focus = max(
                    (samples[item] for item in sample_ids),
                    key=lambda item: float(item.get("value", 0)),
                )["point"]
            stable = hashlib.sha256(
                f"{evaluation.get('evaluation_id')}:{field_ref}:{index}".encode("utf-8")
            ).hexdigest()[:16]
            results.append({
                "patch_id": f"patch_{stable}",
                "evaluation_id": str(evaluation.get("evaluation_id") or ""),
                "measurement_ids": [str(measurement.get("measurement_id") or "")],
                "field_ref": field_ref,
                "scene_ref": str(field.get("scene_ref") or ""),
                "topology_map_ref": str(field.get("topology_map_ref") or ""),
                "topology_snapshot_ref": str(field.get("topology_snapshot_ref") or ""),
                "render_mesh_snapshot_ref": str(field.get("render_mesh_snapshot_ref") or ""),
                "geometry_refs": geometry_refs,
                "region_refs": sorted(str(item) for item in measurement.get("region_refs", [])),
                "feature_refs": sorted(str(item) for item in measurement.get("feature_refs", [])),
                "sample_ids": sample_ids,
                "cell_ids": sorted(str(item.get("cell_id")) for item in cells),
                "triangle_refs": triangles,
                "focus_point": focus,
                "surface_normal": surface_normal,
                "bounds": {
                    "minimum": [min(point[axis] for point in points) for axis in range(3)],
                    "maximum": [max(point[axis] for point in points) for axis in range(3)],
                },
            })
        return results

    @staticmethod
    def _render(
        scene: dict[str, Any],
        patch: dict[str, Any],
        target: Path,
        view: dict[str, Any],
    ) -> None:
        try:
            from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
        except ImportError as exc:
            raise DFMError(
                "evidence_renderer_unavailable",
                "Pillow is required for Hermes field evidence rendering.",
            ) from exc

        final_size = (1440, 810)
        supersample = 2
        width, height = (value * supersample for value in final_size)
        background_top = (249, 250, 252)
        background_bottom = (234, 239, 246)
        panel_top = (247, 250, 253)
        panel_bottom = (226, 233, 242)
        ink = (26, 36, 51)
        muted = (101, 115, 136)
        hairline = (210, 218, 229)
        accent = (229, 72, 66)
        accent_dark = (164, 38, 39)
        accent_pale = (255, 235, 232)

        def font(size: int, *, bold: bool = False) -> Any:
            names = (
                ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
                if bold
                else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
            )
            for name in names:
                try:
                    return ImageFont.truetype(name, size)
                except OSError:
                    continue
            return ImageFont.load_default()

        def vertical_gradient(
            size: tuple[int, int],
            top: tuple[int, int, int],
            bottom: tuple[int, int, int],
        ) -> Any:
            gradient = Image.new("RGB", size, top)
            gradient_draw = ImageDraw.Draw(gradient)
            divisor = max(size[1] - 1, 1)
            for y in range(size[1]):
                amount = y / divisor
                color = tuple(
                    round(start + (end - start) * amount)
                    for start, end in zip(top, bottom)
                )
                gradient_draw.line((0, y, size[0], y), fill=color)
            return gradient

        def paste_rounded(
            canvas: Any,
            layer: Any,
            box: tuple[int, int, int, int],
            *,
            radius: int,
            shadow: bool = True,
        ) -> None:
            left, top, right, bottom = box
            layer_width, layer_height = right - left, bottom - top
            mask = Image.new("L", (layer_width, layer_height), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, layer_width, layer_height), radius=radius, fill=255
            )
            if shadow:
                shadow_mask = Image.new("L", canvas.size, 0)
                ImageDraw.Draw(shadow_mask).rounded_rectangle(
                    (
                        left,
                        top + 8 * supersample,
                        right,
                        bottom + 8 * supersample,
                    ),
                    radius=radius,
                    fill=70,
                )
                shadow_mask = shadow_mask.filter(
                    ImageFilter.GaussianBlur(18 * supersample)
                )
                shadow_layer = Image.new("RGBA", canvas.size, (31, 45, 66, 0))
                shadow_layer.putalpha(shadow_mask)
                canvas.paste(shadow_layer, (0, 0), shadow_layer)
            canvas.paste(layer.resize((layer_width, layer_height)), (left, top), mask)

        highlighted = {
            (
                str(item["render_mesh_snapshot_id"]),
                str(item["primitive_id"]),
                int(item["triangle_id"]),
            )
            for item in patch["triangle_refs"]
        }

        rows: list[dict[str, Any]] = []
        for primitive in scene.get("primitives", []):
            primitive_id = str(primitive.get("primitive_id") or "")
            mesh_snapshot_id = str(primitive.get("render_mesh_snapshot_id") or "")
            vertices = primitive.get("vertices", [])
            for triangle_id, triangle in enumerate(primitive.get("triangles", [])):
                try:
                    points = [vertices[int(index)] for index in triangle]
                except (IndexError, TypeError, ValueError) as exc:
                    raise DFMError(
                        "render_scene_invalid",
                        "A render triangle references a missing vertex.",
                    ) from exc
                edge_a = [points[1][axis] - points[0][axis] for axis in range(3)]
                edge_b = [points[2][axis] - points[0][axis] for axis in range(3)]
                normal = _unit_or_none(_cross(edge_a, edge_b)) or [0.0, 0.0, 1.0]
                rows.append(
                    {
                        "depth": sum(_dot(point, view["basis_d"]) for point in points)
                        / 3.0,
                        "key": (mesh_snapshot_id, primitive_id, triangle_id),
                        "normal": normal,
                        "projected": [
                            (
                                _dot(point, view["basis_u"]),
                                _dot(point, view["basis_v"]),
                            )
                            for point in points
                        ],
                    }
                )
        rows.sort(key=lambda item: item["depth"], reverse=True)
        all_xy = [point for row in rows for point in row["projected"]]
        if not all_xy:
            raise DFMError("render_scene_invalid", "The render scene is empty.")

        focus = patch["focus_point"]
        focus_xy = (
            _dot(focus, view["basis_u"]),
            _dot(focus, view["basis_v"]),
        )
        patch_points = _bounds_corners(patch["bounds"])
        patch_xy = [
            (_dot(point, view["basis_u"]), _dot(point, view["basis_v"]))
            for point in patch_points
        ]
        scene_span = max(
            max(value[0] for value in all_xy) - min(value[0] for value in all_xy),
            max(value[1] for value in all_xy) - min(value[1] for value in all_xy),
            1.0,
        )

        def fit_transform(
            framing: list[tuple[float, float]],
            panel_width: int,
            panel_height: int,
            padding: int,
        ) -> tuple[float, float, float]:
            minimum_x = min(point[0] for point in framing)
            maximum_x = max(point[0] for point in framing)
            minimum_y = min(point[1] for point in framing)
            maximum_y = max(point[1] for point in framing)
            span_x = max(maximum_x - minimum_x, 1e-6)
            span_y = max(maximum_y - minimum_y, 1e-6)
            scale = min(
                (panel_width - 2 * padding) / span_x,
                (panel_height - 2 * padding) / span_y,
            )
            return scale, (minimum_x + maximum_x) / 2, (minimum_y + maximum_y) / 2

        def draw_mesh(
            panel_size: tuple[int, int],
            framing: list[tuple[float, float]],
            *,
            detail: bool,
            callout: str = "",
        ) -> Any:
            panel_width, panel_height = panel_size
            rendered = vertical_gradient(panel_size, panel_top, panel_bottom)
            rendered_draw = ImageDraw.Draw(rendered, "RGBA")
            scale, center_x, center_y = fit_transform(
                framing,
                panel_width,
                panel_height,
                (80 if detail else 30) * supersample,
            )

            def screen(point: tuple[float, float]) -> tuple[int, int]:
                return (
                    round(panel_width / 2 + (point[0] - center_x) * scale),
                    round(panel_height / 2 - (point[1] - center_y) * scale),
                )

            projected_rows = [
                (row, [screen(point) for point in row["projected"]])
                for row in rows
            ]
            model_mask = Image.new("L", panel_size, 0)
            model_mask_draw = ImageDraw.Draw(model_mask)
            shadow_mask = Image.new("L", panel_size, 0)
            shadow_draw = ImageDraw.Draw(shadow_mask)
            for _row, polygon in projected_rows:
                model_mask_draw.polygon(polygon, fill=255)
                shadow_draw.polygon(
                    [
                        (x + 8 * supersample, y + 12 * supersample)
                        for x, y in polygon
                    ],
                    fill=92,
                )
            shadow_mask = shadow_mask.filter(
                ImageFilter.GaussianBlur(16 * supersample)
            )
            shadow_layer = Image.new("RGBA", panel_size, (45, 58, 76, 0))
            shadow_layer.putalpha(shadow_mask)
            rendered = Image.alpha_composite(
                rendered.convert("RGBA"), shadow_layer
            ).convert("RGB")
            rendered_draw = ImageDraw.Draw(rendered, "RGBA")

            light = _unit_or_none([-0.28, -0.48, 0.83])
            assert light is not None
            highlight_polygons = []
            for row, polygon in projected_rows:
                if not _visible(polygon, panel_width, panel_height):
                    continue
                intensity = 0.91 + 0.11 * abs(_dot(row["normal"], light))
                shade = tuple(
                    min(255, round(channel * intensity))
                    for channel in (199, 208, 220)
                )
                rendered_draw.polygon(polygon, fill=(*shade, 255))
                if row["key"] in highlighted:
                    highlight_polygons.append(polygon)

            highlight_mask = Image.new("L", panel_size, 0)
            highlight_mask_draw = ImageDraw.Draw(highlight_mask)
            for polygon in highlight_polygons:
                rendered_draw.polygon(
                    polygon, fill=(*accent, 112 if detail else 72)
                )
                highlight_mask_draw.polygon(polygon, fill=255)

            kernel = 2 * supersample + 1
            silhouette = ImageChops.subtract(
                model_mask.filter(ImageFilter.MaxFilter(kernel)), model_mask
            ).point(lambda value: round(value * (0.58 if detail else 0.44)))
            rendered.paste((67, 82, 103), (0, 0), silhouette)
            highlight_edge = ImageChops.subtract(
                highlight_mask.filter(ImageFilter.MaxFilter(kernel)),
                highlight_mask.filter(ImageFilter.MinFilter(kernel)),
            ).point(lambda value: round(value * 0.68))
            rendered.paste(accent_dark, (0, 0), highlight_edge)
            rendered_draw = ImageDraw.Draw(rendered, "RGBA")

            marker = screen(focus_xy)
            radius = (6 if detail else 5) * supersample
            halo = (13 if detail else 10) * supersample
            rendered_draw.ellipse(
                (
                    marker[0] - halo,
                    marker[1] - halo,
                    marker[0] + halo,
                    marker[1] + halo,
                ),
                fill=(255, 255, 255, 235),
            )
            rendered_draw.ellipse(
                (
                    marker[0] - radius,
                    marker[1] - radius,
                    marker[0] + radius,
                    marker[1] + radius,
                ),
                fill=(*accent, 255),
                outline=(255, 255, 255, 255),
                width=2 * supersample,
            )

            if detail and callout:
                callout_font = font(11 * supersample, bold=True)
                text_box = rendered_draw.textbbox(
                    (0, 0), callout, font=callout_font
                )
                callout_width = text_box[2] - text_box[0] + 28 * supersample
                callout_height = 34 * supersample
                to_right = marker[0] < panel_width * 0.68
                label_left = (
                    marker[0] + 44 * supersample
                    if to_right
                    else marker[0] - callout_width - 44 * supersample
                )
                label_left = max(
                    22 * supersample,
                    min(label_left, panel_width - callout_width - 22 * supersample),
                )
                label_top = max(
                    54 * supersample,
                    min(
                        marker[1] - 48 * supersample,
                        panel_height - callout_height - 36 * supersample,
                    ),
                )
                line_end = (
                    label_left if to_right else label_left + callout_width,
                    label_top + callout_height // 2,
                )
                elbow = (
                    marker[0] + (24 if to_right else -24) * supersample,
                    line_end[1],
                )
                rendered_draw.line(
                    (marker, elbow, line_end),
                    fill=(*accent_dark, 210),
                    width=supersample,
                )
                rendered_draw.rounded_rectangle(
                    (
                        label_left,
                        label_top,
                        label_left + callout_width,
                        label_top + callout_height,
                    ),
                    radius=17 * supersample,
                    fill=(255, 255, 255, 242),
                    outline=(*accent, 100),
                    width=supersample,
                )
                rendered_draw.ellipse(
                    (
                        label_left + 11 * supersample,
                        label_top + 14 * supersample,
                        label_left + 17 * supersample,
                        label_top + 20 * supersample,
                    ),
                    fill=(*accent, 255),
                )
                rendered_draw.text(
                    (
                        label_left + 23 * supersample,
                        label_top + 9 * supersample,
                    ),
                    callout,
                    font=callout_font,
                    fill=ink,
                )
            return rendered

        field_ref = str(patch.get("field_ref") or "").lower()
        if "draft" in field_ref:
            title, callout = "DRAFT ANGLE", "CRITICAL DRAFT REGION"
        elif "thickness" in field_ref:
            title, callout = "WALL THICKNESS", "CRITICAL THICKNESS REGION"
        elif "radius" in field_ref:
            title, callout = "RADIUS CHECK", "CRITICAL RADIUS REGION"
        else:
            title, callout = "GEOMETRY CHECK", "EVALUATED REGION"

        detail_framing = list(patch_xy)
        patch_span_x = max(item[0] for item in patch_xy) - min(
            item[0] for item in patch_xy
        )
        patch_span_y = max(item[1] for item in patch_xy) - min(
            item[1] for item in patch_xy
        )
        minimum_span = scene_span * 0.12
        if view.get("presentation_mode") == "oblique":
            half = scene_span * 0.10
            detail_framing = [
                (focus_xy[0] - half, focus_xy[1] - half),
                (focus_xy[0] + half, focus_xy[1] + half),
            ]
        elif patch_span_x < minimum_span or patch_span_y < minimum_span:
            half = minimum_span / 2
            detail_framing.extend(
                [
                    (focus_xy[0] - half, focus_xy[1] - half),
                    (focus_xy[0] + half, focus_xy[1] + half),
                ]
            )

        canvas = vertical_gradient(
            (width, height), background_top, background_bottom
        )
        context = draw_mesh(
            (330 * supersample, 205 * supersample), all_xy, detail=False
        )
        margin = 32 * supersample
        header_height = 108 * supersample
        hero_width = width - margin * 2
        hero_height = height - header_height - margin
        detail = draw_mesh(
            (hero_width, hero_height),
            detail_framing,
            detail=True,
            callout=callout,
        )
        hero_box = (margin, header_height, width - margin, height - margin)
        paste_rounded(
            canvas,
            detail,
            hero_box,
            radius=22 * supersample,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.rounded_rectangle(
            hero_box,
            radius=22 * supersample,
            outline=(*hairline, 210),
            width=supersample,
        )

        inset_left = margin + 22 * supersample
        inset_bottom = height - margin - 22 * supersample
        inset_box = (
            inset_left,
            inset_bottom - 205 * supersample,
            inset_left + 330 * supersample,
            inset_bottom,
        )
        paste_rounded(
            canvas,
            context,
            inset_box,
            radius=16 * supersample,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.rounded_rectangle(
            inset_box,
            radius=16 * supersample,
            outline=(255, 255, 255, 210),
            width=2 * supersample,
        )

        eyebrow_font = font(10 * supersample, bold=True)
        title_font = font(26 * supersample, bold=True)
        small_font = font(11 * supersample)
        badge_font = font(11 * supersample, bold=True)
        draw.rounded_rectangle(
            (margin, 25 * supersample, margin + 4 * supersample, 80 * supersample),
            radius=2 * supersample,
            fill=accent,
        )
        draw.text(
            (margin + 18 * supersample, 21 * supersample),
            "HERMES  /  DESIGN FOR MANUFACTURING",
            font=eyebrow_font,
            fill=muted,
        )
        draw.text(
            (margin + 18 * supersample, 41 * supersample),
            title,
            font=title_font,
            fill=ink,
        )
        draw.text(
            (margin + 18 * supersample, 77 * supersample),
            f"Evidence {str(patch['patch_id'])[-8:]}  ·  deterministic geometry trace",
            font=small_font,
            fill=muted,
        )

        label = str(view["label"]).upper()
        if view.get("presentation_mode") == "oblique":
            label = f"{label} · OBLIQUE"
        label_box = draw.textbbox((0, 0), label, font=badge_font)
        label_width = label_box[2] - label_box[0] + 28 * supersample
        badge_left = width - margin - label_width
        draw.rounded_rectangle(
            (badge_left, 31 * supersample, width - margin, 65 * supersample),
            radius=17 * supersample,
            fill=ink,
        )
        draw.text(
            (badge_left + 14 * supersample, 40 * supersample),
            label,
            font=badge_font,
            fill=(255, 255, 255),
        )

        status = "ACTION REQUIRED"
        status_box = draw.textbbox((0, 0), status, font=badge_font)
        status_width = status_box[2] - status_box[0] + 42 * supersample
        status_left = badge_left - status_width - 10 * supersample
        draw.rounded_rectangle(
            (
                status_left,
                31 * supersample,
                status_left + status_width,
                65 * supersample,
            ),
            radius=17 * supersample,
            fill=accent_pale,
        )
        draw.ellipse(
            (
                status_left + 13 * supersample,
                45 * supersample,
                status_left + 19 * supersample,
                51 * supersample,
            ),
            fill=accent,
        )
        draw.text(
            (status_left + 26 * supersample, 40 * supersample),
            status,
            font=badge_font,
            fill=accent_dark,
        )

        inset_label_left = inset_left + 13 * supersample
        inset_label_top = inset_box[1] + 13 * supersample
        draw.rounded_rectangle(
            (
                inset_label_left,
                inset_label_top,
                inset_label_left + 98 * supersample,
                inset_label_top + 28 * supersample,
            ),
            radius=14 * supersample,
            fill=(255, 255, 255, 225),
        )
        draw.text(
            (inset_label_left + 13 * supersample, inset_label_top + 7 * supersample),
            "PART CONTEXT",
            font=eyebrow_font,
            fill=muted,
        )

        legend_y = height - margin - 38 * supersample
        cursor = width - margin - 22 * supersample
        legend_font = font(10 * supersample, bold=True)
        for legend_label, color in (
            ("FOCUS", accent),
            ("EVALUATED SURFACE", (224, 118, 111)),
            ("MODEL", (157, 170, 188)),
        ):
            box = draw.textbbox((0, 0), legend_label, font=legend_font)
            item_width = box[2] - box[0] + 24 * supersample
            cursor -= item_width
            draw.ellipse(
                (
                    cursor,
                    legend_y + 5 * supersample,
                    cursor + 8 * supersample,
                    legend_y + 13 * supersample,
                ),
                fill=color,
            )
            draw.text(
                (cursor + 14 * supersample, legend_y),
                legend_label,
                font=legend_font,
                fill=(69, 84, 105),
            )
            cursor -= 16 * supersample

        canvas.resize(final_size, Image.Resampling.LANCZOS).save(
            target, format="PNG"
        )


def _select_representative_patches(
    patches: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Select large patches fairly so one failed metric cannot starve another."""
    if limit <= 0:
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for patch in patches:
        grouped.setdefault(str(patch.get("evaluation_id") or ""), []).append(patch)
    for candidates in grouped.values():
        candidates.sort(
            key=lambda item: (
                len(item.get("cell_ids", [])),
                len(item.get("sample_ids", [])),
                _bounds_volume(item.get("bounds", {})),
                str(item.get("patch_id") or ""),
            ),
            reverse=True,
        )
    selected: list[dict[str, Any]] = []
    while len(selected) < limit:
        added = False
        for candidates in grouped.values():
            if candidates and len(selected) < limit:
                selected.append(candidates.pop(0))
                added = True
        if not added:
            break
    return selected


def _field_pull_direction(
    project_dir: Path, artifacts: list[ArtifactRecord]
) -> list[float] | None:
    for artifact in artifacts:
        if artifact.kind != "scalar_field":
            continue
        field = _read_json(project_dir, artifact)
        context = field.get("calculation_context")
        if not isinstance(context, dict):
            continue
        direction = _unit_or_none(context.get("pull_direction"))
        if direction is not None:
            return direction
    return None


def _adaptive_views(
    scene: dict[str, Any],
    patch: dict[str, Any],
    pull_direction: list[float] | None,
) -> list[dict[str, Any]]:
    """Build three stable camera frames from process and local geometry data."""
    center = _scene_center(scene)
    focus = [float(value) for value in patch["focus_point"]]
    outward = _unit_or_none([focus[i] - center[i] for i in range(3)])
    if outward is None:
        outward = [0.57735027, 0.57735027, 0.57735027]

    surface = _unit_or_none(patch.get("surface_normal")) or outward
    surface = _toward_patch(surface, outward)
    process = _unit_or_none(pull_direction) or _overview_direction(scene, outward)
    process = _toward_patch(process, outward)

    side = _unit_or_none(_cross(process, surface))
    if side is None:
        side = _stable_perpendicular(process)
        surface_view = _unit_or_none(
            [surface[i] + 0.65 * side[i] for i in range(3)]
        ) or surface
    else:
        surface_view = surface
    side = _stable_sign(side)

    return [
        _camera_frame(
            "pull" if pull_direction is not None else "overview",
            "Pull" if pull_direction is not None else "Overview",
            process,
            surface_view,
            "calculation_context.pull_direction"
            if pull_direction is not None
            else "scene_geometry",
        ),
        _camera_frame(
            "surface",
            "Surface",
            surface_view,
            process,
            "failed_patch.surface_normal",
        ),
        _camera_frame("side", "Side", side, process, "derived_orthogonal"),
    ]


def _presentation_view(
    scene: dict[str, Any], patch: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Tilt collapsed orthographic views into readable engineering views."""
    vertices = [
        vertex
        for primitive in scene.get("primitives", [])
        for vertex in primitive.get("vertices", [])
        if isinstance(vertex, list) and len(vertex) == 3
    ]
    if not vertices:
        raise DFMError("render_scene_invalid", "The render scene is empty.")

    patch_points = _bounds_corners(patch["bounds"])
    base_d = source["basis_d"]
    base_u = source["basis_u"]
    base_v = source["basis_v"]

    def candidate_view(horizontal: float, vertical: float) -> dict[str, Any]:
        direction = [
            float(base_d[axis])
            + float(base_u[axis]) * horizontal
            + float(base_v[axis]) * vertical
            for axis in range(3)
        ]
        return _camera_frame(
            str(source["id"]),
            str(source["label"]),
            direction,
            list(base_v),
            str(source["source"]),
        )

    def projected_aspect(
        points: list[list[float]], candidate: dict[str, Any]
    ) -> float:
        projected = [
            (_dot(point, candidate["basis_u"]), _dot(point, candidate["basis_v"]))
            for point in points
        ]
        span_x = max(item[0] for item in projected) - min(item[0] for item in projected)
        span_y = max(item[1] for item in projected) - min(item[1] for item in projected)
        return min(span_x, span_y) / max(span_x, span_y, 1e-9)

    best: tuple[float, float, dict[str, Any]] | None = None
    for horizontal, vertical in (
        (0.0, 0.0),
        (0.22, 0.16),
        (-0.22, 0.16),
        (0.42, 0.24),
        (-0.42, 0.24),
        (0.32, -0.30),
        (-0.32, -0.30),
        (0.82, 0.38),
        (-0.82, 0.38),
        (0.72, -0.62),
        (-0.72, -0.62),
        (1.35, 0.85),
        (-1.35, 0.85),
        (1.50, -1.15),
        (-1.50, -1.15),
    ):
        candidate = candidate_view(horizontal, vertical)
        scene_aspect = projected_aspect(vertices, candidate)
        patch_aspect = projected_aspect(patch_points, candidate)
        deviation = abs(horizontal) + abs(vertical)
        score = scene_aspect * 0.68 + patch_aspect * 0.32 - deviation * 0.012
        if best is None or score > best[0]:
            best = score, scene_aspect, candidate

    assert best is not None
    if best[1] >= 0.24:
        return best[2]

    directions = (
        (
            (-1.0, 1.0, 0.82),
            (-1.0, -1.0, 0.82),
            (-0.72, 1.0, -0.68),
        )
        if str(source.get("label") or "").lower() == "side"
        else (
            (1.0, 1.0, 0.82),
            (1.0, -1.0, 0.82),
            (0.72, 1.0, -0.68),
        )
    )
    for direction in directions:
        candidate = _camera_frame(
            str(source["id"]),
            str(source["label"]),
            list(direction),
            [0.0, 0.0, 1.0],
            str(source["source"]),
        )
        scene_aspect = projected_aspect(vertices, candidate)
        patch_aspect = projected_aspect(patch_points, candidate)
        score = scene_aspect * 0.64 + patch_aspect * 0.36
        if score > best[0]:
            best = score, scene_aspect, candidate
    return {**best[2], "presentation_mode": "oblique"}


def _camera_frame(
    view_id: str,
    label: str,
    direction: list[float],
    up_hint: list[float],
    source: str,
) -> dict[str, Any]:
    basis_d = _unit_or_none(direction)
    assert basis_d is not None
    basis_u = _unit_or_none(_cross(up_hint, basis_d))
    if basis_u is None:
        basis_u = _stable_perpendicular(basis_d)
    basis_v = _unit_or_none(_cross(basis_d, basis_u))
    assert basis_v is not None
    return {
        "id": view_id,
        "label": label,
        "basis_u": tuple(basis_u),
        "basis_v": tuple(basis_v),
        "basis_d": tuple(basis_d),
        "source": source,
    }


def _scene_center(scene: dict[str, Any]) -> list[float]:
    vertices = [
        vertex
        for primitive in scene.get("primitives", [])
        for vertex in primitive.get("vertices", [])
        if isinstance(vertex, list) and len(vertex) == 3
    ]
    if not vertices:
        raise DFMError("render_scene_invalid", "The render scene is empty.")
    return [
        (min(float(item[axis]) for item in vertices) + max(float(item[axis]) for item in vertices))
        / 2.0
        for axis in range(3)
    ]


def _overview_direction(
    scene: dict[str, Any], outward: list[float]
) -> list[float]:
    vertices = [
        vertex
        for primitive in scene.get("primitives", [])
        for vertex in primitive.get("vertices", [])
        if isinstance(vertex, list) and len(vertex) == 3
    ]
    spans = [
        max(float(item[axis]) for item in vertices)
        - min(float(item[axis]) for item in vertices)
        for axis in range(3)
    ]
    smallest_axis = min(range(3), key=lambda axis: spans[axis])
    thin_axis = [0.0, 0.0, 0.0]
    thin_axis[smallest_axis] = 1.0
    direction = _unit_or_none(
        [outward[i] + 0.75 * thin_axis[i] for i in range(3)]
    )
    return direction or outward


def _average_direction(values: list[Any]) -> list[float] | None:
    directions = [item for value in values if (item := _unit_or_none(value))]
    if not directions:
        return None
    reference = directions[0]
    aligned = [
        direction if _dot(direction, reference) >= 0 else [-item for item in direction]
        for direction in directions
    ]
    return _unit_or_none(
        [sum(direction[axis] for direction in aligned) for axis in range(3)]
    )


def _unit_or_none(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    length = math.sqrt(sum(item * item for item in vector))
    if length <= 1e-12:
        return None
    return [item / length for item in vector]


def _cross(left: Any, right: Any) -> list[float]:
    return [
        float(left[1]) * float(right[2]) - float(left[2]) * float(right[1]),
        float(left[2]) * float(right[0]) - float(left[0]) * float(right[2]),
        float(left[0]) * float(right[1]) - float(left[1]) * float(right[0]),
    ]


def _stable_perpendicular(direction: list[float]) -> list[float]:
    axis = min(range(3), key=lambda index: abs(direction[index]))
    helper = [0.0, 0.0, 0.0]
    helper[axis] = 1.0
    perpendicular = _unit_or_none(_cross(direction, helper))
    assert perpendicular is not None
    return _stable_sign(perpendicular)


def _toward_patch(direction: list[float], outward: list[float]) -> list[float]:
    return direction if _dot(direction, outward) >= 0 else [-item for item in direction]


def _stable_sign(direction: list[float]) -> list[float]:
    for item in direction:
        if abs(item) > 1e-12:
            return direction if item > 0 else [-value for value in direction]
    return direction


def _bounds_corners(bounds: dict[str, Any]) -> list[list[float]]:
    minimum = bounds["minimum"]
    maximum = bounds["maximum"]
    return [
        [x, y, z]
        for x in (minimum[0], maximum[0])
        for y in (minimum[1], maximum[1])
        for z in (minimum[2], maximum[2])
    ]


def _bounds_volume(bounds: dict[str, Any]) -> float:
    try:
        minimum = bounds["minimum"]
        maximum = bounds["maximum"]
        return math.prod(max(0.0, float(maximum[i]) - float(minimum[i])) for i in range(3))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _read_json(project_dir: Path, artifact: ArtifactRecord) -> dict[str, Any]:
    try:
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DFMError(
            "evidence_artifact_invalid",
            f"The {artifact.kind} artifact is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            "evidence_artifact_invalid",
            f"The {artifact.kind} artifact must be a JSON object.",
        )
    return payload


def _connected_cells(
    cells: list[dict[str, Any]], scene: dict[str, Any]
) -> list[list[dict[str, Any]]]:
    triangle_vertices = {
        (
            str(primitive.get("render_mesh_snapshot_id")),
            str(primitive.get("primitive_id")),
            triangle_id,
        ): {
            tuple(round(float(value), 9) for value in vertices[int(vertex_id)])
            for vertex_id in triangle
        }
        for primitive in scene.get("primitives", [])
        for vertices in [primitive.get("vertices", [])]
        for triangle_id, triangle in enumerate(primitive.get("triangles", []))
    }

    def mesh_vertices(cell: dict[str, Any]) -> set[tuple[float, ...]]:
        ref = cell.get("triangle_ref", {})
        return triangle_vertices.get(
            (
                str(ref.get("render_mesh_snapshot_id")),
                str(ref.get("primitive_id")),
                int(ref.get("triangle_id", -1)),
            ),
            set(),
        )

    remaining = list(cells)
    groups: list[list[dict[str, Any]]] = []
    while remaining:
        group = [remaining.pop()]
        sample_ids = set(str(item) for item in group[0].get("sample_ids", []))
        vertices = set(mesh_vertices(group[0]))
        changed = True
        while changed:
            changed = False
            for cell in list(remaining):
                cell_samples = set(str(item) for item in cell.get("sample_ids", []))
                cell_vertices = mesh_vertices(cell)
                if sample_ids.intersection(cell_samples) or len(
                    vertices.intersection(cell_vertices)
                ) >= 2:
                    remaining.remove(cell)
                    group.append(cell)
                    sample_ids.update(cell_samples)
                    vertices.update(cell_vertices)
                    changed = True
        groups.append(group)
    return groups


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"))
        unique[key] = value
    return list(unique.values())


def _stable_content_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _dot(left: list[float], right: tuple[float, float, float]) -> float:
    return sum(float(left[index]) * right[index] for index in range(3))


def _visible(points: list[tuple[int, int]], width: int, height: int) -> bool:
    return not (
        max(point[0] for point in points) < 0
        or min(point[0] for point in points) >= width
        or max(point[1] for point in points) < 0
        or min(point[1] for point in points) >= height
    )
