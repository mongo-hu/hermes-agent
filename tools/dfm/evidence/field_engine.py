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

    version = "hermes-field-evidence-v4"

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
            if evaluation.get("expression") is not None:
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
                self._render(scene, patch, image_path, view)
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
                            "viewport": [1280, 720],
                            "patch_id": patch["patch_id"],
                            "scene_ref": patch["scene_ref"],
                            "topology_snapshot_ref": patch["topology_snapshot_ref"],
                            "render_mesh_snapshot_ref": patch["render_mesh_snapshot_ref"],
                            "view_id": view["id"],
                            "view_label": view["label"],
                            "camera_direction": list(view["basis_d"]),
                            "camera_up": list(view["basis_v"]),
                            "camera_source": view["source"],
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
        mesh_payload = [
            {key: value for key, value in item.items() if key != "render_mesh_snapshot_id"}
            for item in primitives
        ]
        topology_payload = [
            {
                "entity_id": face.get("geometry_ref", {}).get("entity_id"),
                "kind": face.get("geometry_ref", {}).get("kind"),
                "index": face.get("geometry_ref", {}).get("index"),
            }
            for face in linked_payloads["topology_map"].get("faces", [])
        ]
        if (
            scene_snapshot.get("render_mesh_sha256") != _stable_content_sha256(mesh_payload)
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
            from PIL import Image, ImageDraw
        except ImportError as exc:
            raise DFMError(
                "evidence_renderer_unavailable",
                "Pillow is required for Hermes field evidence rendering.",
            ) from exc

        width, height = 1280, 720
        image = Image.new("RGB", (width, height), (247, 248, 250))
        draw = ImageDraw.Draw(image)
        highlighted = {
            (
                str(item["render_mesh_snapshot_id"]),
                str(item["primitive_id"]),
                int(item["triangle_id"]),
            )
            for item in patch["triangle_refs"]
        }
        basis_u = view["basis_u"]
        basis_v = view["basis_v"]
        basis_d = view["basis_d"]

        projected: list[tuple[float, str, int, list[tuple[float, float]]]] = []
        all_xy: list[tuple[float, float]] = []
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
                xy = [(_dot(point, basis_u), _dot(point, basis_v)) for point in points]
                depth = sum(_dot(point, basis_d) for point in points) / 3.0
                projected.append((depth, primitive_id, triangle_id, xy))
                all_xy.extend(xy)
        if not all_xy:
            raise DFMError("render_scene_invalid", "The render scene is empty.")

        focus = patch["focus_point"]
        focus_xy = (_dot(focus, basis_u), _dot(focus, basis_v))
        patch_points = _bounds_corners(patch["bounds"])
        patch_xy = [(_dot(point, basis_u), _dot(point, basis_v)) for point in patch_points]
        scene_span = max(
            max(value[0] for value in all_xy) - min(value[0] for value in all_xy),
            max(value[1] for value in all_xy) - min(value[1] for value in all_xy),
            1.0,
        )
        patch_span = max(
            max(value[0] for value in patch_xy) - min(value[0] for value in patch_xy),
            max(value[1] for value in patch_xy) - min(value[1] for value in patch_xy),
            scene_span * 0.06,
        )
        scale = min(width, height) * 0.62 / (patch_span * 2.5)

        def screen(point: tuple[float, float]) -> tuple[int, int]:
            return (
                round(width / 2 + (point[0] - focus_xy[0]) * scale),
                round(height / 2 - (point[1] - focus_xy[1]) * scale),
            )

        visible_highlights: list[list[tuple[int, int]]] = []
        for _depth, primitive_id, triangle_id, xy in sorted(projected, reverse=True):
            polygon = [screen(point) for point in xy]
            if not _visible(polygon, width, height):
                continue
            is_failed = (mesh_snapshot_id, primitive_id, triangle_id) in highlighted
            draw.polygon(
                polygon,
                fill=(210, 214, 220),
                outline=(151, 158, 168),
            )
            if is_failed:
                visible_highlights.append(polygon)

        # Draw the evaluated patch last so an internal or rear-facing problem is
        # still locatable. This is an evidence overlay, not a hidden-line claim.
        for polygon in visible_highlights:
            draw.polygon(polygon, fill=(225, 48, 48), outline=(125, 12, 12))
            draw.line([*polygon, polygon[0]], fill=(125, 12, 12), width=4)

        marker = screen(focus_xy)
        halo_radius = 24
        radius = 17
        draw.ellipse(
            (
                marker[0] - halo_radius,
                marker[1] - halo_radius,
                marker[0] + halo_radius,
                marker[1] + halo_radius,
            ),
            fill=(255, 255, 255),
            outline=(255, 255, 255),
            width=4,
        )
        draw.ellipse(
            (
                marker[0] - radius,
                marker[1] - radius,
                marker[0] + radius,
                marker[1] + radius,
            ),
            fill=(220, 25, 25),
            outline=(120, 8, 8),
            width=4,
        )
        draw.line(
            (marker[0] - 30, marker[1], marker[0] + 30, marker[1]),
            fill=(120, 8, 8),
            width=3,
        )
        draw.line(
            (marker[0], marker[1] - 30, marker[0], marker[1] + 30),
            fill=(120, 8, 8),
            width=3,
        )
        label = str(view["label"])
        draw.rounded_rectangle((24, 22, 154, 62), radius=8, fill=(31, 41, 55))
        draw.text((42, 34), label, fill=(255, 255, 255))
        image.save(target, format="PNG")


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
