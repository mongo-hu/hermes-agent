"""Export PythonOCC reference calculations through the backend-neutral contract."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...contracts import GeometryRef, MeasurementRecord, PlanOperation, RegionRecord
from ...errors import DFMError
from . import legacy_analyzer as legacy


SCHEMA_VERSION = 2
ALGORITHM_VERSION = "pythonocc-demo-field-v2"
SCENE_ARTIFACT_ID = "scene_geometry"
TOPOLOGY_ARTIFACT_ID = "topology_geometry"
_DEFLECTION_MM = 0.8
_THICKNESS_SAMPLE_LIMIT = 4000
_MIN_HIT_MM = 0.01


def _content_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def export_objective_fields(
    input_path: Path,
    *,
    run_id: str,
    input_sha256: str,
    operations: list[PlanOperation],
    regions: list[RegionRecord],
) -> dict[str, Any]:
    """Calculate only objective wall-thickness and draft artifacts."""

    by_calculator: dict[str, list[PlanOperation]] = {}
    for operation in operations:
        by_calculator.setdefault(operation.calculator_id, []).append(operation)
    load_operation = next(iter(by_calculator.get("load_geometry", [])), None)
    model_unit_argument = (
        load_operation.arguments.get("model_unit") if load_operation else None
    )
    model_unit = str(
        model_unit_argument.value if model_unit_argument is not None else ""
    ).lower()
    if model_unit != "mm":
        raise DFMError(
            "objective_unit_unsupported",
            "The frozen objective geometry contract requires millimeter input.",
            {"model_unit": model_unit or None, "supported_units": ["mm"]},
        )
    occ = legacy.import_occ()
    shape = legacy.read_step(input_path, occ)
    mesh = _mesh(shape, occ, input_sha256)
    topology_content_sha256 = _content_sha256(
        [
            {
                "entity_id": item["geometry_ref"]["entity_id"],
                "kind": item["geometry_ref"]["kind"],
                "index": item["geometry_ref"]["index"],
            }
            for item in mesh
        ]
    )
    topology_snapshot_id = f"topology_{topology_content_sha256[:16]}"
    for item in mesh:
        item["geometry_ref"]["topology_snapshot_id"] = topology_snapshot_id
    scene_primitives = [item["primitive"] for item in mesh]
    render_mesh_sha256 = _content_sha256(scene_primitives)
    render_mesh_snapshot_id = f"mesh_{render_mesh_sha256[:16]}"
    for primitive in scene_primitives:
        primitive["render_mesh_snapshot_id"] = render_mesh_snapshot_id
    topology_snapshot = {
        "topology_snapshot_id": topology_snapshot_id,
        "input_sha256": input_sha256,
        "backend": "pythonocc",
        "backend_version": ALGORITHM_VERSION,
        "loader_id": "pythonocc-step-loader",
        "loader_version": ALGORITHM_VERSION,
        "indexer_id": "pythonocc-face-indexer",
        "indexer_version": "1",
        "entity_count": {"body": 1, "face": len(mesh)},
        "topology_content_sha256": topology_content_sha256,
    }
    render_mesh_snapshot = {
        "render_mesh_snapshot_id": render_mesh_snapshot_id,
        "topology_snapshot_id": topology_snapshot_id,
        "input_sha256": input_sha256,
        "producer": "pythonocc",
        "producer_version": ALGORITHM_VERSION,
        "tessellation": {"linear_deflection_mm": _DEFLECTION_MM},
        "triangle_count": sum(len(item["triangles"]) for item in scene_primitives),
        "render_mesh_sha256": render_mesh_sha256,
    }
    scene = {
        "schema_version": SCHEMA_VERSION,
        "scene_id": SCENE_ARTIFACT_ID,
        "run_id": run_id,
        "input_sha256": input_sha256,
        "coordinate_system": "model",
        "unit": model_unit,
        "topology_snapshot_ref": topology_snapshot_id,
        "render_mesh_snapshot": render_mesh_snapshot,
        "primitives": scene_primitives,
    }
    topology = {
        "schema_version": SCHEMA_VERSION,
        "map_id": TOPOLOGY_ARTIFACT_ID,
        "run_id": run_id,
        "input_sha256": input_sha256,
        "scene_ref": SCENE_ARTIFACT_ID,
        "topology_snapshot": topology_snapshot,
        "render_mesh_snapshot_ref": render_mesh_snapshot_id,
        "faces": [
            {
                "geometry_ref": item["geometry_ref"],
                "triangle_refs": [
                    {
                        "primitive_id": item["primitive"]["primitive_id"],
                        "triangle_id": triangle_id,
                        "render_mesh_snapshot_id": render_mesh_snapshot_id,
                    }
                    for triangle_id in range(len(item["primitive"]["triangles"]))
                ],
            }
            for item in mesh
        ],
    }

    fields: list[tuple[str, dict[str, Any]]] = []
    measurements: list[MeasurementRecord] = []
    if topology_operations := by_calculator.get("inspect_topology"):
        operation = topology_operations[0]
        if operation.metric_ids and operation.required_quantities:
            measurements.append(
                MeasurementRecord(
                    measurement_id="measurement-valid_brep",
                    operation_id=operation.operation_id,
                    calculator_id=operation.calculator_id,
                    metric_id=operation.metric_ids[0],
                    quantity_id=operation.required_quantities[0],
                    value=True,
                    unit=None,
                    status="measured",
                    geometry_refs=[],
                    region_refs=list(operation.region_refs),
                    feature_refs=list(operation.feature_refs),
                    method="pythonocc_brep_load",
                    algorithm_version=ALGORITHM_VERSION,
                    input_sha256=input_sha256,
                    quality={"backend": "pythonocc_demo", "certified": False},
                    diagnostics={"face_count": len(mesh)},
                )
            )
    region_by_id = {item.region_id: item for item in regions}
    for operation in by_calculator.get("measure_wall_thickness", []):
        target_mesh = _operation_mesh(mesh, operation, region_by_id, input_sha256)
        field_id = _field_id("wall_thickness", operation)
        field, measurement = _thickness_field(
            shape, occ, target_mesh, operation, run_id, input_sha256, field_id
        )
        fields.append((field_id, field))
        measurements.append(measurement)
    for operation in by_calculator.get("measure_draft", []):
        target_mesh = _operation_mesh(mesh, operation, region_by_id, input_sha256)
        field_id = _field_id("draft", operation)
        pull = operation.arguments.get("pull_direction")
        pull_direction = pull.value if pull is not None else [0, 0, 1]
        field, measurement = _draft_field(
            target_mesh,
            operation,
            run_id,
            input_sha256,
            field_id,
            pull_direction,
        )
        fields.append((field_id, field))
        measurements.append(measurement)

    required = [item for item in operations if item.metric_ids]
    if len(measurements) != len(required):
        raise DFMError(
            "calculation_failed",
            "PythonOCC did not produce every objective measurement in the plan.",
        )
    return {
        "scene": scene,
        "topology": topology,
        "fields": fields,
        "measurements": measurements,
    }


def _field_id(kind: str, operation: PlanOperation) -> str:
    suffix = operation.operation_id.rsplit(".", 1)[-1]
    return f"field_{kind}_{suffix}"


def _operation_mesh(
    mesh: list[dict[str, Any]],
    operation: PlanOperation,
    regions: dict[str, RegionRecord],
    input_sha256: str,
) -> list[dict[str, Any]]:
    if len(operation.region_refs) != 1:
        raise DFMError(
            "objective_region_invalid",
            "Each regional objective operation must reference exactly one region.",
            {"operation_id": operation.operation_id},
        )
    region = regions.get(operation.region_refs[0])
    if region is None or region.input_sha256 != input_sha256:
        raise DFMError(
            "objective_region_invalid",
            "The objective operation region does not match the input geometry.",
            {"operation_id": operation.operation_id},
        )
    included = {(item.kind, item.index) for item in region.geometry_refs}
    excluded = {(item.kind, item.index) for item in region.excluded_geometry_refs}
    selected = []
    for item in mesh:
        ref = item["geometry_ref"]
        key = (str(ref["kind"]), int(ref["index"]))
        keep = region.mode == "whole_model"
        if region.mode == "topology_refs":
            keep = key in included
        elif region.mode == "topology_complement":
            keep = key not in excluded
        elif region.mode == "bbox" and region.bbox is not None:
            minimum, maximum = region.bbox.minimum, region.bbox.maximum
            triangles = []
            triangle_ids = []
            source_ids = item.get("triangle_ids") or list(
                range(len(item["primitive"]["triangles"]))
            )
            for local_id, triangle in enumerate(item["primitive"]["triangles"]):
                center = [
                    sum(item["vertices"][index][axis] for index in triangle) / 3.0
                    for axis in range(3)
                ]
                if all(minimum[axis] <= center[axis] <= maximum[axis] for axis in range(3)):
                    triangles.append(triangle)
                    triangle_ids.append(source_ids[local_id])
            if triangles:
                copied = dict(item)
                copied["primitive"] = {**item["primitive"], "triangles": triangles}
                copied["triangle_ids"] = triangle_ids
                selected.append(copied)
            continue
        if keep:
            selected.append(item)
    if not selected:
        raise DFMError(
            "objective_region_empty",
            "The objective region resolved to no model topology.",
            {"operation_id": operation.operation_id, "region_id": region.region_id},
        )
    return selected


def _mesh(shape: Any, occ: SimpleNamespace, input_sha256: str) -> list[dict[str, Any]]:
    occ.BRepMesh_IncrementalMesh(shape, _DEFLECTION_MM)
    results: list[dict[str, Any]] = []
    for face_index, raw_face in enumerate(
        legacy.iter_shapes(shape, occ.TopAbs_FACE, occ), start=1
    ):
        face = occ.topods.Face(raw_face)
        location = occ.TopLoc_Location()
        triangulation = occ.BRep_Tool.Triangulation(face, location)
        if triangulation is None:
            continue
        transform = location.Transformation()
        vertices: list[list[float]] = []
        uvs: list[list[float] | None] = []
        for node_index in range(1, triangulation.NbNodes() + 1):
            point = triangulation.Node(node_index)
            if not location.IsIdentity():
                point = point.Transformed(transform)
            vertices.append([float(point.X()), float(point.Y()), float(point.Z())])
            uvs.append(_uv_node(triangulation, node_index))
        triangles: list[list[int]] = []
        for triangle_index in range(1, triangulation.NbTriangles() + 1):
            ids = [int(value) - 1 for value in triangulation.Triangle(triangle_index).Get()]
            if face.Orientation() == occ.TopAbs_REVERSED:
                ids = [ids[0], ids[2], ids[1]]
            triangles.append(ids)
        if not vertices or not triangles:
            continue
        primitive_id = f"face-{face_index}"
        results.append(
            {
                "face": face,
                "location": location,
                "surface": occ.BRepAdaptor_Surface(face),
                "vertices": vertices,
                "uvs": uvs,
                "primitive": {
                    "primitive_id": primitive_id,
                    "vertices": vertices,
                    "triangles": triangles,
                },
                "triangle_ids": list(range(len(triangles))),
                "geometry_ref": GeometryRef(
                    "face", face_index, input_sha256,
                    entity_id=f"face_{face_index:06d}",
                ).to_dict(),
            }
        )
    if not results:
        raise DFMError("calculation_failed", "PythonOCC produced an empty render mesh.")
    return results


def _uv_node(triangulation: Any, node_index: int) -> list[float] | None:
    try:
        if hasattr(triangulation, "HasUVNodes") and not triangulation.HasUVNodes():
            return None
        uv = triangulation.UVNode(node_index)
        return [float(uv.X()), float(uv.Y())]
    except (AttributeError, RuntimeError):
        return None


def _draft_field(
    mesh: list[dict[str, Any]],
    operation: PlanOperation,
    run_id: str,
    input_sha256: str,
    field_id: str,
    pull_direction: Any,
) -> tuple[dict[str, Any], MeasurementRecord]:
    pull = _unit([float(value) for value in pull_direction])
    samples: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    values: list[float] = []
    fallback_count = 0
    for item in mesh:
        primitive = item["primitive"]
        normal_by_vertex = _vertex_normals(item)
        sample_ids: dict[int, str] = {}
        used_vertices = sorted({index for triangle in primitive["triangles"] for index in triangle})
        for vertex_id in used_vertices:
            point = item["vertices"][vertex_id]
            uv = item["uvs"][vertex_id]
            normal = _surface_normal(item, uv)
            if normal is None:
                normal = normal_by_vertex[vertex_id]
                fallback_count += 1
            value = math.degrees(
                math.asin(min(1.0, max(0.0, abs(_dot(normal, pull)))))
            )
            sample_id = f"draft-{primitive['primitive_id']}-v{vertex_id}"
            sample_ids[vertex_id] = sample_id
            values.append(value)
            samples.append(
                {
                    "sample_id": sample_id,
                    "point": point,
                    "uv": uv,
                    "surface_normal": normal,
                    "value": value,
                    "geometry_ref": item["geometry_ref"],
                    "mesh_vertex_ref": {
                        "primitive_id": primitive["primitive_id"],
                        "vertex_id": vertex_id,
                        "render_mesh_snapshot_id": primitive["render_mesh_snapshot_id"],
                    },
                }
            )
        triangle_ids = item.get("triangle_ids") or list(range(len(primitive["triangles"])))
        for local_triangle_id, triangle in enumerate(primitive["triangles"]):
            triangle_id = triangle_ids[local_triangle_id]
            cells.append(
                {
                    "cell_id": f"draft-{primitive['primitive_id']}-t{triangle_id}",
                    "sample_ids": [sample_ids[index] for index in triangle],
                    "geometry_ref": item["geometry_ref"],
                    "triangle_ref": {
                        "primitive_id": primitive["primitive_id"],
                        "triangle_id": triangle_id,
                        "render_mesh_snapshot_id": primitive["render_mesh_snapshot_id"],
                    },
                }
            )
    return _field_and_measurement(
        operation,
        run_id,
        input_sha256,
        field_id,
        "draft_angle_deg",
        "degree",
        "linear_on_triangle",
        samples,
        cells,
        values,
        {
            "normal_source": "occt_surface_uv_with_mesh_fallback",
            "mesh_normal_fallback_count": fallback_count,
            "pull_direction": pull,
        },
        {"pull_direction": pull},
    )


def _thickness_field(
    shape: Any,
    occ: SimpleNamespace,
    mesh: list[dict[str, Any]],
    operation: PlanOperation,
    run_id: str,
    input_sha256: str,
    field_id: str,
) -> tuple[dict[str, Any], MeasurementRecord]:
    try:
        import vtk
    except ImportError as exc:
        raise DFMError(
            "dependency_missing", "VTK is required for PythonOCC wall-thickness sampling."
        ) from exc
    polydata = legacy.triangulate_shape(shape, occ, _DEFLECTION_MM)
    tree = vtk.vtkOBBTree()
    tree.SetDataSet(polydata)
    tree.BuildLocator()
    ray_length = legacy.bounds_diag(legacy.shape_bbox(shape, occ)) * 2.5
    triangle_count = sum(len(item["primitive"]["triangles"]) for item in mesh)
    stride = max(1, math.ceil(triangle_count / _THICKNESS_SAMPLE_LIMIT))
    samples: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    values: list[float] = []
    ordinal = 0
    for item in mesh:
        primitive = item["primitive"]
        triangle_ids = item.get("triangle_ids") or list(range(len(primitive["triangles"])))
        for local_triangle_id, triangle in enumerate(primitive["triangles"]):
            triangle_id = triangle_ids[local_triangle_id]
            ordinal += 1
            if (ordinal - 1) % stride:
                continue
            points = [item["vertices"][index] for index in triangle]
            normal = _triangle_normal(points)
            center = [sum(point[axis] for point in points) / 3.0 for axis in range(3)]
            value = legacy.estimate_thickness_at_point(
                tree, tuple(center), tuple(normal), ray_length, _MIN_HIT_MM
            )
            if value is None or not math.isfinite(value):
                continue
            sample_id = f"thickness-{primitive['primitive_id']}-t{triangle_id}"
            values.append(float(value))
            samples.append(
                {
                    "sample_id": sample_id,
                    "point": center,
                    "uv": None,
                    "surface_normal": normal,
                    "value": float(value),
                    "geometry_ref": item["geometry_ref"],
                    "mesh_vertex_ref": None,
                }
            )
            cells.append(
                {
                    "cell_id": sample_id,
                    "sample_ids": [sample_id],
                    "geometry_ref": item["geometry_ref"],
                    "triangle_ref": {
                        "primitive_id": primitive["primitive_id"],
                        "triangle_id": triangle_id,
                        "render_mesh_snapshot_id": primitive["render_mesh_snapshot_id"],
                    },
                }
            )
    if not values:
        raise DFMError(
            "calculation_failed",
            "PythonOCC could not obtain a valid wall-thickness ray intersection.",
        )
    return _field_and_measurement(
        operation,
        run_id,
        input_sha256,
        field_id,
        "thickness_mm",
        "mm",
        "constant_per_triangle",
        samples,
        cells,
        values,
        {"ray_mode": "bidirectional_first_hit", "sample_stride": stride},
        {},
    )


def _field_and_measurement(
    operation: PlanOperation,
    run_id: str,
    input_sha256: str,
    field_id: str,
    quantity_id: str,
    unit: str,
    interpolation: str,
    samples: list[dict[str, Any]],
    cells: list[dict[str, Any]],
    values: list[float],
    diagnostics: dict[str, Any],
    calculation_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], MeasurementRecord]:
    if not values or not operation.metric_ids:
        raise DFMError("calculation_failed", "Objective scalar field has no samples.")
    minimum = min(values)
    quality = {
        "backend": "pythonocc_demo",
        "certified": False,
        "mesh_deflection_mm": _DEFLECTION_MM,
        "sample_count": len(samples),
        "includes_controlling_extrema": False,
    }
    field = {
        "schema_version": SCHEMA_VERSION,
        "field_id": field_id,
        "run_id": run_id,
        "input_sha256": input_sha256,
        "operation_id": operation.operation_id,
        "metric_id": operation.metric_ids[0],
        "quantity_id": quantity_id,
        "unit": unit,
        "scene_ref": SCENE_ARTIFACT_ID,
        "topology_map_ref": TOPOLOGY_ARTIFACT_ID,
        "topology_snapshot_ref": samples[0]["geometry_ref"]["topology_snapshot_id"],
        "render_mesh_snapshot_ref": cells[0]["triangle_ref"]["render_mesh_snapshot_id"],
        "interpolation": interpolation,
        "calculation_context": calculation_context or {},
        "samples": samples,
        "cells": cells,
        "quality": quality,
        "feature_refs": list(operation.feature_refs),
        "region_refs": list(operation.region_refs),
    }
    controlling = min(samples, key=lambda item: float(item["value"]))
    measurement = MeasurementRecord(
        measurement_id=f"measurement-{quantity_id}-{operation.operation_id.rsplit('.', 1)[-1]}",
        operation_id=operation.operation_id,
        calculator_id=operation.calculator_id,
        metric_id=operation.metric_ids[0],
        quantity_id=quantity_id,
        value=minimum,
        unit=unit,
        status="measured",
        geometry_refs=[GeometryRef.from_dict(controlling["geometry_ref"])],
        method="pythonocc_triangulated_field",
        algorithm_version=ALGORITHM_VERSION,
        input_sha256=input_sha256,
        quality=quality,
        diagnostics={**diagnostics, "minimum_point": controlling["point"]},
        field_refs=[field_id],
        feature_refs=list(operation.feature_refs),
        region_refs=list(operation.region_refs),
    )
    return field, measurement


def _surface_normal(item: dict[str, Any], uv: list[float] | None) -> list[float] | None:
    if uv is None:
        return None
    try:
        from OCC.Core.BRepLProp import BRepLProp_SLProps
        from OCC.Core.TopAbs import TopAbs_REVERSED

        props = BRepLProp_SLProps(item["surface"], uv[0], uv[1], 1, 1e-7)
        if not props.IsNormalDefined():
            return None
        direction = props.Normal()
        normal = [float(direction.X()), float(direction.Y()), float(direction.Z())]
        if item["face"].Orientation() == TopAbs_REVERSED:
            normal = [-value for value in normal]
        return _unit(normal)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _vertex_normals(item: dict[str, Any]) -> list[list[float]]:
    totals = [[0.0, 0.0, 0.0] for _ in item["vertices"]]
    for triangle in item["primitive"]["triangles"]:
        normal = _triangle_normal([item["vertices"][index] for index in triangle])
        for index in triangle:
            totals[index] = [totals[index][axis] + normal[axis] for axis in range(3)]
    return [_unit(value) for value in totals]


def _triangle_normal(points: list[list[float]]) -> list[float]:
    first = [points[1][axis] - points[0][axis] for axis in range(3)]
    second = [points[2][axis] - points[0][axis] for axis in range(3)]
    return _unit(
        [
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        ]
    )


def _unit(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1e-12:
        raise DFMError("calculation_failed", "Geometry calculation produced a zero normal.")
    return [value / length for value in vector]


def _dot(left: list[float], right: list[float]) -> float:
    return sum(left[index] * right[index] for index in range(3))
