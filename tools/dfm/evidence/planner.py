"""Deterministic scene preparation and coverage-first evidence view planning."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from ..errors import DFMError


Vector3 = tuple[float, float, float]
Point2 = tuple[float, float]
TriangleKey = tuple[str, str, int]
_MAX_NORMAL_CANDIDATES = 24


@dataclass(frozen=True)
class PreparedTriangle:
    """One immutable render triangle with view-independent data."""

    key: TriangleKey
    points: tuple[Vector3, Vector3, Vector3]
    normal: Vector3


@dataclass(frozen=True)
class PreparedScene:
    """Render-scene geometry normalized once for all evidence views."""

    triangles: tuple[PreparedTriangle, ...]
    vertices: tuple[Vector3, ...]
    triangle_by_key: dict[TriangleKey, PreparedTriangle]
    center: Vector3
    spans: Vector3


@dataclass(frozen=True)
class ProjectedTriangle:
    """One triangle projected into an orthographic camera frame."""

    key: TriangleKey
    depth: float
    normal: Vector3
    projected: tuple[Point2, Point2, Point2]


@dataclass(frozen=True)
class ProjectedScene:
    """Projection and depth ordering shared by every overlay for one camera."""

    rows: tuple[ProjectedTriangle, ...]
    all_xy: tuple[Point2, ...]


@dataclass(frozen=True)
class EvidenceViewPlan:
    """A deterministic camera plus the complete patch set it must show."""

    view: dict[str, Any]
    patch_ids: tuple[str, ...]
    visibility_mode: str = "xray_overlay"


@dataclass(frozen=True)
class PatchProjection:
    """View-dependent patch geometry cached for the whole planning pass."""

    bounds: tuple[float, float, float, float]
    center: Point2
    quality: float


def prepare_scene(scene: dict[str, Any]) -> PreparedScene:
    """Validate and normalize a render scene without any camera-dependent work."""

    triangles: list[PreparedTriangle] = []
    vertices_flat: list[Vector3] = []
    triangle_by_key: dict[TriangleKey, PreparedTriangle] = {}
    for primitive in scene.get("primitives", []):
        primitive_id = str(primitive.get("primitive_id") or "")
        mesh_snapshot_id = str(primitive.get("render_mesh_snapshot_id") or "")
        raw_vertices = primitive.get("vertices", [])
        vertices: list[Vector3] = []
        for raw in raw_vertices:
            if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                raise DFMError(
                    "render_scene_invalid", "A render vertex must contain three coordinates."
                )
            try:
                vertex = tuple(float(value) for value in raw)
            except (TypeError, ValueError) as exc:
                raise DFMError(
                    "render_scene_invalid", "A render vertex contains a non-numeric coordinate."
                ) from exc
            if not all(math.isfinite(value) for value in vertex):
                raise DFMError(
                    "render_scene_invalid", "A render vertex contains a non-finite coordinate."
                )
            vertices.append(vertex)
        vertices_flat.extend(vertices)

        for triangle_id, raw_triangle in enumerate(primitive.get("triangles", [])):
            try:
                point_values = tuple(vertices[int(index)] for index in raw_triangle)
            except (IndexError, TypeError, ValueError) as exc:
                raise DFMError(
                    "render_scene_invalid",
                    "A render triangle references a missing vertex.",
                ) from exc
            if len(point_values) != 3:
                raise DFMError(
                    "render_scene_invalid", "A render triangle must contain three vertices."
                )
            points = (point_values[0], point_values[1], point_values[2])
            edge_a = _subtract(points[1], points[0])
            edge_b = _subtract(points[2], points[0])
            normal = _unit(_cross(edge_a, edge_b)) or (0.0, 0.0, 1.0)
            key = (mesh_snapshot_id, primitive_id, triangle_id)
            triangle = PreparedTriangle(key=key, points=points, normal=normal)
            triangles.append(triangle)
            triangle_by_key[key] = triangle

    if not vertices_flat or not triangles:
        raise DFMError("render_scene_invalid", "The render scene is empty.")

    minimum = tuple(min(point[axis] for point in vertices_flat) for axis in range(3))
    maximum = tuple(max(point[axis] for point in vertices_flat) for axis in range(3))
    center = tuple((minimum[axis] + maximum[axis]) / 2.0 for axis in range(3))
    spans = tuple(maximum[axis] - minimum[axis] for axis in range(3))
    return PreparedScene(
        triangles=tuple(triangles),
        vertices=tuple(vertices_flat),
        triangle_by_key=triangle_by_key,
        center=center,
        spans=spans,
    )


def project_scene(prepared: PreparedScene, view: dict[str, Any]) -> ProjectedScene:
    """Project and depth-sort a prepared scene exactly once for one camera."""

    basis_u = _vector(view.get("basis_u"))
    basis_v = _vector(view.get("basis_v"))
    basis_d = _vector(view.get("basis_d"))
    if basis_u is None or basis_v is None or basis_d is None:
        raise DFMError("evidence_view_invalid", "An evidence camera basis is invalid.")

    rows = [
        ProjectedTriangle(
            key=triangle.key,
            depth=sum(_dot(point, basis_d) for point in triangle.points) / 3.0,
            normal=triangle.normal,
            projected=tuple(
                (_dot(point, basis_u), _dot(point, basis_v))
                for point in triangle.points
            ),
        )
        for triangle in prepared.triangles
    ]
    rows.sort(key=lambda item: item.depth, reverse=True)
    return ProjectedScene(
        rows=tuple(rows),
        all_xy=tuple(point for row in rows for point in row.projected),
    )


def projection_cache_key(view: dict[str, Any]) -> tuple[float, ...]:
    """Return a stable camera key independent of labels and assigned patches."""

    return tuple(
        round(float(value), 12)
        for name in ("basis_u", "basis_v", "basis_d")
        for value in view[name]
    )


def plan_evidence_views(
    prepared: PreparedScene,
    patches: list[dict[str, Any]],
    pull_direction: list[float] | None,
    *,
    max_markers_per_image: int,
    viewport: tuple[int, int] = (1376, 638),
    minimum_patch_pixels: int = 24,
) -> list[EvidenceViewPlan]:
    """Cover every patch with a small deterministic set of readable views."""

    if not patches:
        return []
    marker_limit = max(1, max_markers_per_image)
    candidates = _candidate_views(prepared, patches, pull_direction)
    patches_by_id = {
        str(patch.get("patch_id") or ""): patch
        for patch in patches
        if patch.get("patch_id")
    }
    points_by_patch = {
        patch_id: _patch_points(prepared, patch)
        for patch_id, patch in patches_by_id.items()
    }
    projected_by_candidate = [
        (
            candidate,
            {
                patch_id: _project_patch(
                    points_by_patch[patch_id],
                    patch,
                    candidate,
                )
                for patch_id, patch in patches_by_id.items()
            },
        )
        for candidate in candidates
    ]
    uncovered = set(patches_by_id)
    plans: list[EvidenceViewPlan] = []
    ordinal_by_camera: dict[str, int] = {}

    while uncovered:
        best: tuple[tuple[int, int, float, str], dict[str, Any], tuple[str, ...]] | None = None
        for candidate, projections in projected_by_candidate:
            group = _best_patch_group(
                [patches_by_id[patch_id] for patch_id in sorted(uncovered)],
                projections,
                marker_limit=marker_limit,
                viewport=viewport,
                minimum_patch_pixels=minimum_patch_pixels,
            )
            if not group:
                continue
            sample_count = sum(
                len(patches_by_id[patch_id].get("sample_ids", []))
                for patch_id in group
            )
            quality = sum(
                projections[patch_id].quality
                for patch_id in group
            )
            score = (sample_count, len(group), quality, str(candidate["id"]))
            if best is None or score > best[0]:
                best = score, candidate, group

        if best is None:
            patch_id = sorted(uncovered)[0]
            patch = patches_by_id[patch_id]
            candidate = _local_view(prepared, patch, pull_direction)
            group = (patch_id,)
        else:
            _score, candidate, group = best

        camera_id = str(candidate["id"])
        ordinal_by_camera[camera_id] = ordinal_by_camera.get(camera_id, 0) + 1
        plan_view = {
            **candidate,
            "id": f"{camera_id}-{ordinal_by_camera[camera_id]:02d}",
            "camera_id": camera_id,
            "presentation_mode": "local",
        }
        plans.append(EvidenceViewPlan(view=plan_view, patch_ids=group))
        uncovered.difference_update(group)

    return plans


def _candidate_views(
    prepared: PreparedScene,
    patches: list[dict[str, Any]],
    pull_direction: list[float] | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    process = _unit(pull_direction) if pull_direction is not None else None
    if process is not None:
        candidates.append(
            _camera_frame(
                "pull",
                "Pull",
                process,
                (0.0, 0.0, 1.0),
                "calculation_context.pull_direction",
            )
        )

    standard = (
        ("iso-ppp", "Isometric", (1.0, 1.0, 0.82)),
        ("iso-npp", "Isometric", (-1.0, 1.0, 0.82)),
        ("iso-pnp", "Isometric", (1.0, -1.0, 0.82)),
        ("iso-nnp", "Isometric", (-1.0, -1.0, 0.82)),
        ("axis-x", "X", (1.0, 0.0, 0.0)),
        ("axis-y", "Y", (0.0, 1.0, 0.0)),
        ("axis-z", "Z", (0.0, 0.0, 1.0)),
        ("axis-nx", "-X", (-1.0, 0.0, 0.0)),
        ("axis-ny", "-Y", (0.0, -1.0, 0.0)),
        ("axis-nz", "-Z", (0.0, 0.0, -1.0)),
    )
    for view_id, label, direction in standard:
        candidates.append(
            _camera_frame(view_id, label, direction, (0.0, 0.0, 1.0), "standard")
        )

    normal_buckets: dict[tuple[int, int, int], tuple[dict[str, Any], Vector3]] = {}
    for patch in sorted(patches, key=lambda item: str(item.get("patch_id") or "")):
        normal = _unit(patch.get("surface_normal"))
        if normal is None:
            continue
        outward = _unit(
            tuple(
                float(patch["focus_point"][axis]) - prepared.center[axis]
                for axis in range(3)
            )
        )
        if outward is not None and _dot(normal, outward) < 0:
            normal = tuple(-value for value in normal)
        bucket = tuple(round(value * 4) for value in normal)
        normal_buckets.setdefault(bucket, (patch, normal))

    for patch, normal in list(normal_buckets.values())[:_MAX_NORMAL_CANDIDATES]:
        candidates.append(
            _camera_frame(
                f"normal-{str(patch['patch_id'])[-8:]}",
                "Surface",
                normal,
                process or (0.0, 0.0, 1.0),
                "failed_patch.surface_normal",
            )
        )

    unique: dict[tuple[float, ...], dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(projection_cache_key(candidate), candidate)
    return list(unique.values())


def _best_patch_group(
    patches: list[dict[str, Any]],
    projections: dict[str, PatchProjection],
    *,
    marker_limit: int,
    viewport: tuple[int, int],
    minimum_patch_pixels: int,
) -> tuple[str, ...]:
    compatible = [
        patch
        for patch in patches
        if projections[str(patch["patch_id"])].quality > 0.01
    ]
    if not compatible:
        return ()

    best: tuple[tuple[int, int, float, str], tuple[str, ...]] | None = None
    for seed in compatible:
        seed_center = projections[str(seed["patch_id"])].center
        ordered = sorted(
            compatible,
            key=lambda patch: (
                _distance_2d(
                    seed_center,
                    projections[str(patch["patch_id"])].center,
                ),
                str(patch.get("patch_id") or ""),
            ),
        )
        group: list[dict[str, Any]] = []
        for patch in ordered:
            if len(group) >= marker_limit:
                break
            proposed = [*group, patch]
            if _group_is_legible(
                proposed,
                projections,
                viewport=viewport,
                minimum_patch_pixels=minimum_patch_pixels,
            ):
                group = proposed
        if not group:
            continue
        patch_ids = tuple(str(patch["patch_id"]) for patch in group)
        sample_count = sum(len(patch.get("sample_ids", [])) for patch in group)
        quality = sum(projections[str(patch["patch_id"])].quality for patch in group)
        score = (sample_count, len(group), quality, ":".join(patch_ids))
        if best is None or score > best[0]:
            best = score, patch_ids
    return best[1] if best is not None else ()


def _group_is_legible(
    patches: list[dict[str, Any]],
    projections: dict[str, PatchProjection],
    *,
    viewport: tuple[int, int],
    minimum_patch_pixels: int,
) -> bool:
    projected = [
        projections[str(patch["patch_id"])].bounds
        for patch in patches
    ]
    minimum_x = min(bounds[0] for bounds in projected)
    maximum_x = max(bounds[1] for bounds in projected)
    minimum_y = min(bounds[2] for bounds in projected)
    maximum_y = max(bounds[3] for bounds in projected)
    span_x = max(maximum_x - minimum_x, 1e-6)
    span_y = max(maximum_y - minimum_y, 1e-6)
    padding = 80
    scale = min(
        max(1, viewport[0] - 2 * padding) / span_x,
        max(1, viewport[1] - 2 * padding) / span_y,
    )
    for patch, bounds in zip(patches, projected):
        patch_span = max(bounds[1] - bounds[0], bounds[3] - bounds[2]) * scale
        if patch.get("triangle_refs") and patch_span < minimum_patch_pixels:
            return False
    return True


def _patch_view_quality(
    prepared: PreparedScene, patch: dict[str, Any], view: dict[str, Any]
) -> float:
    bounds = _projected_patch_bounds(prepared, patch, view)
    return _patch_view_quality_from_bounds(bounds, patch, view)


def _patch_view_quality_from_bounds(
    bounds: tuple[float, float, float, float],
    patch: dict[str, Any],
    view: dict[str, Any],
) -> float:
    span_x = max(0.0, bounds[1] - bounds[0])
    span_y = max(0.0, bounds[3] - bounds[2])
    aspect = min(span_x, span_y) / max(span_x, span_y, 1e-9)
    normal = _unit(patch.get("surface_normal"))
    direction = _vector(view.get("basis_d"))
    facing = abs(_dot(normal, direction)) if normal is not None and direction is not None else 0.5
    if not patch.get("triangle_refs"):
        return 0.5 + facing
    return aspect * 0.6 + facing * 0.4


def _project_patch(
    points: tuple[Vector3, ...],
    patch: dict[str, Any],
    view: dict[str, Any],
) -> PatchProjection:
    """Project one patch once for a candidate camera."""

    basis_u = _vector(view.get("basis_u"))
    basis_v = _vector(view.get("basis_v"))
    assert basis_u is not None and basis_v is not None
    projected = [(_dot(point, basis_u), _dot(point, basis_v)) for point in points]
    bounds = (
        min(point[0] for point in projected),
        max(point[0] for point in projected),
        min(point[1] for point in projected),
        max(point[1] for point in projected),
    )
    center = (
        (bounds[0] + bounds[1]) / 2.0,
        (bounds[2] + bounds[3]) / 2.0,
    )
    return PatchProjection(
        bounds=bounds,
        center=center,
        quality=_patch_view_quality_from_bounds(bounds, patch, view),
    )


def _projected_patch_bounds(
    prepared: PreparedScene, patch: dict[str, Any], view: dict[str, Any]
) -> tuple[float, float, float, float]:
    basis_u = _vector(view.get("basis_u"))
    basis_v = _vector(view.get("basis_v"))
    assert basis_u is not None and basis_v is not None
    points = _patch_points(prepared, patch)
    projected = [(_dot(point, basis_u), _dot(point, basis_v)) for point in points]
    return (
        min(point[0] for point in projected),
        max(point[0] for point in projected),
        min(point[1] for point in projected),
        max(point[1] for point in projected),
    )


def _projected_patch_center(
    prepared: PreparedScene, patch: dict[str, Any], view: dict[str, Any]
) -> Point2:
    bounds = _projected_patch_bounds(prepared, patch, view)
    return ((bounds[0] + bounds[1]) / 2.0, (bounds[2] + bounds[3]) / 2.0)


def _patch_points(prepared: PreparedScene, patch: dict[str, Any]) -> tuple[Vector3, ...]:
    points: list[Vector3] = []
    for ref in patch.get("triangle_refs", []):
        try:
            key = (
                str(ref["render_mesh_snapshot_id"]),
                str(ref["primitive_id"]),
                int(ref["triangle_id"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        triangle = prepared.triangle_by_key.get(key)
        if triangle is not None:
            points.extend(triangle.points)
    if points:
        return tuple(points)
    bounds = patch.get("bounds", {})
    minimum = bounds.get("minimum")
    maximum = bounds.get("maximum")
    if (
        isinstance(minimum, (list, tuple))
        and isinstance(maximum, (list, tuple))
        and len(minimum) == 3
        and len(maximum) == 3
    ):
        return tuple(
            (float(x), float(y), float(z))
            for x in (minimum[0], maximum[0])
            for y in (minimum[1], maximum[1])
            for z in (minimum[2], maximum[2])
        )
    focus = _vector(patch.get("focus_point"))
    if focus is None:
        raise DFMError("evidence_patch_invalid", "An evidence patch has no valid geometry.")
    return (focus,)


def _local_view(
    prepared: PreparedScene,
    patch: dict[str, Any],
    pull_direction: list[float] | None,
) -> dict[str, Any]:
    focus = _vector(patch.get("focus_point")) or prepared.center
    outward = _unit(tuple(focus[axis] - prepared.center[axis] for axis in range(3)))
    normal = _unit(patch.get("surface_normal")) or outward or (1.0, 1.0, 1.0)
    if outward is not None and _dot(normal, outward) < 0:
        normal = tuple(-value for value in normal)
    process = _unit(pull_direction) if pull_direction is not None else None
    return _camera_frame(
        f"local-{str(patch.get('patch_id') or '')[-8:]}",
        "Local",
        normal,
        process or (0.0, 0.0, 1.0),
        "coverage_fallback",
    )


def _camera_frame(
    view_id: str,
    label: str,
    direction: Iterable[float],
    up_hint: Iterable[float],
    source: str,
) -> dict[str, Any]:
    basis_d = _unit(direction)
    if basis_d is None:
        raise DFMError("evidence_view_invalid", "An evidence camera direction is invalid.")
    up = _unit(up_hint) or (0.0, 0.0, 1.0)
    basis_u = _unit(_cross(up, basis_d))
    if basis_u is None:
        basis_u = _stable_perpendicular(basis_d)
    basis_v = _unit(_cross(basis_d, basis_u))
    assert basis_v is not None
    return {
        "id": view_id,
        "label": label,
        "basis_u": basis_u,
        "basis_v": basis_v,
        "basis_d": basis_d,
        "source": source,
    }


def _stable_perpendicular(direction: Vector3) -> Vector3:
    axis = min(range(3), key=lambda index: abs(direction[index]))
    helper = [0.0, 0.0, 0.0]
    helper[axis] = 1.0
    perpendicular = _unit(_cross(direction, helper))
    assert perpendicular is not None
    return perpendicular


def _distance_2d(left: Point2, right: Point2) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])


def _subtract(left: Vector3, right: Vector3) -> Vector3:
    return tuple(left[index] - right[index] for index in range(3))


def _cross(left: Iterable[float], right: Iterable[float]) -> Vector3:
    left_values = tuple(float(value) for value in left)
    right_values = tuple(float(value) for value in right)
    return (
        left_values[1] * right_values[2] - left_values[2] * right_values[1],
        left_values[2] * right_values[0] - left_values[0] * right_values[2],
        left_values[0] * right_values[1] - left_values[1] * right_values[0],
    )


def _dot(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(float(a) * float(b) for a, b in zip(left, right))


def _vector(value: Any) -> Vector3 | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return vector if all(math.isfinite(item) for item in vector) else None


def _unit(value: Any) -> Vector3 | None:
    vector = _vector(value)
    if vector is None:
        return None
    length = math.sqrt(sum(item * item for item in vector))
    if length <= 1e-12:
        return None
    return tuple(item / length for item in vector)
