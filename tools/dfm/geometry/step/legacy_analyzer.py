#!/usr/bin/env python
"""Compatibility copy of the proven Django DFM STEP analyzer.

Migration source:
E:/workspace/mongo-hu/django-vue3-admin/backend/aimold_app/agents/
skill/dfm-analysis/scripts/dfm_analyze.py

Keep geometry measurement and rule changes out of this module until approved
by the M1 comparison baseline. Runtime progress and bounded evidence scheduling
remain covered by parity tests; process orchestration belongs in the worker.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from ...reporting.legacy_reports import write_json_report, write_markdown_report
from .model import LoadedStepModel
from .evidence import EvidenceResult, render_evidence_bundle
from .checks import (
    continuity,
    cylindrical,
    face_quality,
    indexed_features,
    planar_spacing,
    thickness,
    undercut,
)


Vec3 = tuple[float, float, float]
BBox = tuple[float, float, float, float, float, float]
ScreenBBox = tuple[int, int, int, int]
DFM_EVENT_PREFIX = "__DFM_EVENT__ "


def emit_dfm_event(event: str, **payload: Any) -> None:
    print(
        DFM_EVENT_PREFIX + json.dumps({"event": event, **payload}, ensure_ascii=True),
        flush=True,
    )


def emit_artifact_event(path: Path, artifact_type: str = "image") -> None:
    emit_dfm_event(
        "artifact",
        type=artifact_type,
        name=path.name,
        path=str(path),
    )


def emit_report_delta(markdown: str) -> None:
    if markdown:
        emit_dfm_event("report_delta", markdown=markdown)


@dataclass
class Issue:
    id: str
    code: str
    title: str
    severity: str
    message: str
    anchor: Vec3 | None = None
    metric: dict[str, Any] = field(default_factory=dict)
    refs: list[dict[str, int]] = field(default_factory=list)
    view_dir: Vec3 | None = None
    image: str | None = None
    images: list[str] = field(default_factory=list)


@dataclass
class PlaneFace:
    face_index: int
    normal: Vec3
    point: Vec3
    offset: float
    area: float
    bbox: BBox
    center: Vec3


@dataclass
class FaceInfo:
    face_index: int
    kind: str
    normal: Vec3 | None
    center: Vec3
    area: float
    bbox: BBox
    curvature: float | None = None
    axis: Vec3 | None = None
    axis_point: Vec3 | None = None
    radius: float | None = None


@dataclass
class ScreenAnnotation:
    point: tuple[int, int]
    radius: int
    target_bbox: ScreenBBox | None = None
    adjusted: bool = False


def add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a: Vec3, scalar: float) -> Vec3:
    return (a[0] * scalar, a[1] * scalar, a[2] * scalar)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec3, fallback: Vec3 = (0.0, 0.0, 1.0)) -> Vec3:
    length = norm(a)
    if length <= 1e-12:
        return fallback
    return (a[0] / length, a[1] / length, a[2] / length)


def distance(a: Vec3, b: Vec3) -> float:
    return norm(sub(a, b))


def gp_to_tuple(point: Any) -> Vec3:
    return (float(point.X()), float(point.Y()), float(point.Z()))


def dir_to_tuple(direction: Any) -> Vec3:
    return unit((float(direction.X()), float(direction.Y()), float(direction.Z())))


def parse_vec3(value: str) -> Vec3:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "Expected three comma-separated values, e.g. 0,0,1"
        )
    try:
        return unit((float(parts[0]), float(parts[1]), float(parts[2])))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_vec3_config(value: Any) -> Vec3:
    if isinstance(value, str):
        return parse_vec3(value)
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return unit((float(value[0]), float(value[1]), float(value[2])))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    raise argparse.ArgumentTypeError(
        "Expected a vector string like '0,0,1' or a three-number list."
    )


@contextmanager
def suppress_native_output() -> Iterable[None]:
    if os.getenv("DFM_VERBOSE_STEP_EXPORT"):
        yield
        return
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        old_stdout = os.dup(1)
        old_stderr = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
        finally:
            os.dup2(old_stdout, 1)
            os.dup2(old_stderr, 2)
            os.close(old_stdout)
            os.close(old_stderr)


def import_occ() -> SimpleNamespace:
    try:
        from OCC.Core.Bnd import Bnd_Box
        from OCC.Core.BRep import BRep_Tool
        from OCC.Core.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
        from OCC.Core.BRepBndLib import brepbndlib
        from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
        from OCC.Core.BRepCheck import BRepCheck_Analyzer
        from OCC.Core.BRepExtrema import BRepExtrema_DistShapeShape
        from OCC.Core.BRepGProp import brepgprop
        from OCC.Core.BRepLProp import BRepLProp_SLProps
        from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
        from OCC.Core.BRepTools import BRepTools_WireExplorer, breptools
        from OCC.Core.GProp import GProp_GProps
        from OCC.Core.GeomAbs import (
            GeomAbs_Circle,
            GeomAbs_Cylinder,
            GeomAbs_Line,
            GeomAbs_Plane,
        )
        from OCC.Core.gp import gp_Pnt
        from OCC.Core.IFSelect import IFSelect_RetDone
        from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB
        from OCC.Core.STEPCAFControl import STEPCAFControl_Writer
        from OCC.Core.STEPControl import STEPControl_Reader
        from OCC.Core.TDocStd import TDocStd_Document
        from OCC.Core.TopAbs import (
            TopAbs_EDGE,
            TopAbs_FACE,
            TopAbs_REVERSED,
            TopAbs_VERTEX,
        )
        from OCC.Core.TopExp import TopExp_Explorer, topexp
        from OCC.Core.TopLoc import TopLoc_Location
        from OCC.Core.TopTools import (
            TopTools_IndexedDataMapOfShapeListOfShape,
            TopTools_ListIteratorOfListOfShape,
        )
        from OCC.Core.TopoDS import topods
        from OCC.Core.XCAFDoc import (
            XCAFDoc_ColorCurv,
            XCAFDoc_ColorGen,
            XCAFDoc_ColorSurf,
            XCAFDoc_DocumentTool,
        )
    except ImportError as exc:
        raise RuntimeError(
            "pythonOCC/OpenCascade is not importable in the current Django Python environment. "
            "Run scripts/install_dependencies.py from this skill or preinstall pythonocc-core "
            "in the deployment image."
        ) from exc

    return SimpleNamespace(**locals())


def read_step(path: Path, occ: SimpleNamespace) -> Any:
    reader = occ.STEPControl_Reader()
    status = reader.ReadFile(str(path))
    if status != occ.IFSelect_RetDone:
        raise RuntimeError(f"OpenCascade failed to read STEP file: {path}")
    transferred = reader.TransferRoots()
    if transferred <= 0:
        raise RuntimeError(
            f"OpenCascade read the file but transferred no roots: {path}"
        )
    return reader.OneShape()


def iter_shapes(shape: Any, kind: Any, occ: SimpleNamespace) -> Iterable[Any]:
    explorer = occ.TopExp_Explorer(shape, kind)
    while explorer.More():
        yield explorer.Current()
        explorer.Next()


def shape_bbox(shape: Any, occ: SimpleNamespace) -> BBox:
    box = occ.Bnd_Box()
    occ.brepbndlib.Add(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (
        float(xmin),
        float(ymin),
        float(zmin),
        float(xmax),
        float(ymax),
        float(zmax),
    )


def bbox_size(bbox: BBox) -> Vec3:
    return (bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2])


def bbox_center(bbox: BBox) -> Vec3:
    return (
        (bbox[0] + bbox[3]) / 2.0,
        (bbox[1] + bbox[4]) / 2.0,
        (bbox[2] + bbox[5]) / 2.0,
    )


def bbox_dimensions(bbox: BBox) -> Vec3:
    return (
        max(0.0, bbox[3] - bbox[0]),
        max(0.0, bbox[4] - bbox[1]),
        max(0.0, bbox[5] - bbox[2]),
    )


def bbox_extent_along_dir(bbox: BBox, direction: Vec3) -> float:
    direction = unit(direction)
    size = bbox_dimensions(bbox)
    return (
        abs(direction[0]) * size[0]
        + abs(direction[1]) * size[1]
        + abs(direction[2]) * size[2]
    )


def dominant_axis_index(direction: Vec3) -> int:
    direction = unit(direction)
    values = [abs(direction[0]), abs(direction[1]), abs(direction[2])]
    return max(range(3), key=lambda index: values[index])


def bbox_perpendicular_spans(bbox: BBox, direction: Vec3) -> list[float]:
    size = bbox_dimensions(bbox)
    dominant = dominant_axis_index(direction)
    return [size[index] for index in range(3) if index != dominant]


def principal_axis(index: int, sign: float = 1.0) -> Vec3:
    values = [0.0, 0.0, 0.0]
    values[index] = 1.0 if sign >= 0 else -1.0
    return (values[0], values[1], values[2])


def bbox_overlap_on_axes(
    a: BBox, b: BBox, axes: list[int], tolerance: float = 1e-6
) -> bool:
    mins_a = (a[0], a[1], a[2])
    maxs_a = (a[3], a[4], a[5])
    mins_b = (b[0], b[1], b[2])
    maxs_b = (b[3], b[4], b[5])
    for axis in axes:
        if maxs_a[axis] < mins_b[axis] - tolerance:
            return False
        if maxs_b[axis] < mins_a[axis] - tolerance:
            return False
    return True


def bbox_overlap_lengths_on_axes(
    a: BBox, b: BBox, axes: list[int], tolerance: float = 1e-6
) -> list[float]:
    mins_a = (a[0], a[1], a[2])
    maxs_a = (a[3], a[4], a[5])
    mins_b = (b[0], b[1], b[2])
    maxs_b = (b[3], b[4], b[5])
    lengths = []
    for axis in axes:
        low = max(mins_a[axis], mins_b[axis])
        high = min(maxs_a[axis], maxs_b[axis])
        lengths.append(max(0.0, high - low + tolerance))
    return lengths


def percentile_from_sorted(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * percent / 100.0)))
    return values[index]


def face_props(face: Any, occ: SimpleNamespace) -> tuple[float, Vec3]:
    props = occ.GProp_GProps()
    occ.brepgprop.SurfaceProperties(face, props)
    return float(props.Mass()), gp_to_tuple(props.CentreOfMass())


def edge_props(edge: Any, occ: SimpleNamespace) -> tuple[float, Vec3]:
    props = occ.GProp_GProps()
    occ.brepgprop.LinearProperties(edge, props)
    return float(props.Mass()), gp_to_tuple(props.CentreOfMass())


def surface_kind_name(surf_type: Any, occ: SimpleNamespace) -> str:
    if surf_type == occ.GeomAbs_Plane:
        return "plane"
    if surf_type == occ.GeomAbs_Cylinder:
        return "cylinder"
    return str(surf_type)


def sampled_face_normal(face: Any, surf: Any, occ: SimpleNamespace) -> Vec3 | None:
    try:
        u = (float(surf.FirstUParameter()) + float(surf.LastUParameter())) / 2.0
        v = (float(surf.FirstVParameter()) + float(surf.LastVParameter())) / 2.0
        props = occ.BRepLProp_SLProps(surf, u, v, 1, 1e-6)
        if not props.IsNormalDefined():
            return None
        return dir_to_tuple(props.Normal())
    except Exception:
        return None


def face_surface_info(
    face: Any,
    surf: Any,
    surf_type: Any,
    face_index: int,
    area: float,
    center: Vec3,
    face_bbox: BBox,
    occ: SimpleNamespace,
) -> FaceInfo:
    kind = surface_kind_name(surf_type, occ)
    normal: Vec3 | None = None
    curvature: float | None = None
    axis_dir: Vec3 | None = None
    axis_point: Vec3 | None = None
    radius: float | None = None

    if surf_type == occ.GeomAbs_Plane:
        plane = surf.Plane()
        normal = dir_to_tuple(plane.Axis().Direction())
    elif surf_type == occ.GeomAbs_Cylinder:
        cylinder = surf.Cylinder()
        radius = float(cylinder.Radius())
        axis_dir = dir_to_tuple(cylinder.Axis().Direction())
        axis_point = gp_to_tuple(cylinder.Axis().Location())
        center_from_axis = sub(center, axis_point)
        radial = sub(center_from_axis, mul(axis_dir, dot(center_from_axis, axis_dir)))
        normal = unit(radial, perpendicular_vector(axis_dir))
        curvature = 1.0 / radius if radius > 1e-9 else None
    else:
        normal = sampled_face_normal(face, surf, occ)

    if normal is not None and face.Orientation() == occ.TopAbs_REVERSED:
        normal = mul(normal, -1.0)

    return FaceInfo(
        face_index=face_index,
        kind=kind,
        normal=normal,
        center=center,
        area=area,
        bbox=face_bbox,
        curvature=curvature,
        axis=axis_dir,
        axis_point=axis_point,
        radius=radius,
    )


def count_topology(shape: Any, occ: SimpleNamespace) -> dict[str, int]:
    return {
        "faces": sum(1 for _ in iter_shapes(shape, occ.TopAbs_FACE, occ)),
        "edges": sum(1 for _ in iter_shapes(shape, occ.TopAbs_EDGE, occ)),
        "vertices": sum(1 for _ in iter_shapes(shape, occ.TopAbs_VERTEX, occ)),
    }


def shape_volume(shape: Any, occ: SimpleNamespace) -> float | None:
    try:
        props = occ.GProp_GProps()
        occ.brepgprop.VolumeProperties(shape, props)
        return float(props.Mass())
    except Exception:
        return None


def shape_area(shape: Any, occ: SimpleNamespace) -> float | None:
    try:
        props = occ.GProp_GProps()
        occ.brepgprop.SurfaceProperties(shape, props)
        return float(props.Mass())
    except Exception:
        return None


def is_valid_shape(shape: Any, occ: SimpleNamespace) -> bool:
    return bool(occ.BRepCheck_Analyzer(shape).IsValid())


def probe_backends() -> dict[str, Any]:
    python_prefix = Path(sys.prefix)
    freecad_cmd = python_prefix / "Library" / "bin" / "freecadcmd.exe"
    cadquery_version = None
    if importlib.util.find_spec("cadquery"):
        try:
            import cadquery as cq

            cadquery_version = getattr(cq, "__version__", "installed")
        except Exception as exc:
            cadquery_version = f"import failed: {exc}"
    return {
        "python": sys.executable,
        "pythonocc": bool(importlib.util.find_spec("OCC")),
        "cadquery": cadquery_version,
        "ocp": bool(importlib.util.find_spec("OCP")),
        "vtk": bool(importlib.util.find_spec("vtk")),
        "freecadcmd": str(freecad_cmd)
        if freecad_cmd.exists()
        else shutil.which("freecadcmd"),
    }


def make_issue(
    issues: list[Issue],
    counters: dict[str, int],
    code: str,
    title: str,
    severity: str,
    message: str,
    anchor: Vec3 | None,
    metric: dict[str, Any],
    _max_per_code: int | None,
    refs: list[dict[str, int]] | None = None,
    view_dir: Vec3 | None = None,
) -> None:
    counters[code] = counters.get(code, 0) + 1
    issue_id = f"DFM-{len(issues) + 1:03d}"
    issues.append(
        Issue(
            issue_id,
            code,
            title,
            severity,
            message,
            anchor,
            metric,
            refs or [],
            unit(view_dir) if view_dir is not None else None,
        )
    )


def operation_enabled(args: argparse.Namespace, *operations: str) -> bool:
    selected = set(getattr(args, "operation", None) or [])
    return not selected or bool(selected.intersection(operations))


def analyze_shape(
    shape: Any,
    occ: SimpleNamespace,
    args: argparse.Namespace,
    out_dir: Path | None = None,
) -> tuple[list[Issue], dict[str, Any]]:
    issues: list[Issue] = []
    counters: dict[str, int] = {}
    model = LoadedStepModel.build(shape, occ)
    bbox = model.bbox
    stats = dict(model.stats)

    emit_model_summary(stats)

    stage_title = "基础几何与小特征扫描"
    emit_stage_start(stage_title)
    stage_issue_start = len(issues)

    if not stats["valid_brep"]:
        make_issue(
            issues,
            counters,
            "invalid_brep",
            "模型拓扑无效",
            "high",
            "OpenCascade BRepCheck 判定模型存在拓扑/几何有效性问题，建议先在 CAD 中修复再做量产评审。",
            stats["bbox_center"],
            {},
            args.max_issues_per_code,
        )

    plane_faces: list[PlaneFace] = list(model.plane_faces)
    face_infos: list[FaceInfo] = [entry.info for entry in model.faces]
    indexed_features.run(model, occ, args, issues, counters)

    emit_stage_result(
        stage_title,
        issues[stage_issue_start:],
        shape=shape,
        occ=occ,
        out_dir=out_dir,
        args=args,
        global_bbox=bbox,
    )

    stage_title = "平面台阶与加工间隙"
    emit_stage_start(stage_title)
    stage_issue_start = len(issues)
    if operation_enabled(args, "measure_planar_spacing"):
        planar_spacing.run(shape, occ, plane_faces, issues, counters, args)
    emit_stage_result(
        stage_title,
        issues[stage_issue_start:],
        shape=shape,
        occ=occ,
        out_dir=out_dir,
        args=args,
        global_bbox=bbox,
    )

    stage_title = "面质量与碎面"
    emit_stage_start(stage_title)
    stage_issue_start = len(issues)
    stats["face_quality"] = (
        face_quality.run(face_infos, issues, counters, args)
        if operation_enabled(args, "inspect_face_quality")
        else {"enabled": False, "small_face_count": 0, "sliver_face_count": 0}
    )
    emit_stage_result(
        stage_title,
        issues[stage_issue_start:],
        shape=shape,
        occ=occ,
        out_dir=out_dir,
        args=args,
        global_bbox=bbox,
    )

    stage_title = "孔、圆柱与孔边距"
    emit_stage_start(stage_title)
    stage_issue_start = len(issues)
    stats["cylindrical_dfm"] = (
        cylindrical.run(shape, occ, face_infos, bbox, issues, counters, args)
        if operation_enabled(args, "inspect_cylindrical_features")
        else {"enabled": False}
    )
    emit_stage_result(
        stage_title,
        issues[stage_issue_start:],
        shape=shape,
        occ=occ,
        out_dir=out_dir,
        args=args,
        global_bbox=bbox,
    )

    if (
        operation_enabled(args, "measure_wall_thickness")
        and args.enable_thickness_field
    ):
        stage_title = "壁厚场与厚薄突变"
        emit_stage_start(stage_title)
        stage_issue_start = len(issues)
        stats["thickness_field"] = thickness.run(shape, occ, issues, counters, args)
        emit_stage_result(
            stage_title,
            issues[stage_issue_start:],
            shape=shape,
            occ=occ,
            out_dir=out_dir,
            args=args,
            global_bbox=bbox,
        )
    if (
        operation_enabled(args, "inspect_surface_continuity")
        and args.enable_surface_continuity
    ):
        stage_title = "曲面连续性"
        emit_stage_start(stage_title)
        stage_issue_start = len(issues)
        stats["surface_continuity"] = continuity.run(
            shape, occ, face_infos, issues, counters, args
        )
        emit_stage_result(
            stage_title,
            issues[stage_issue_start:],
            shape=shape,
            occ=occ,
            out_dir=out_dir,
            args=args,
            global_bbox=bbox,
        )
    if operation_enabled(args, "inspect_undercut") and args.enable_undercut_slider:
        stage_title = "倒扣、拔模与侧抽"
        emit_stage_start(stage_title)
        stage_issue_start = len(issues)
        stats["undercut_slider"] = undercut.run(
            shape, occ, face_infos, issues, counters, args
        )
        emit_stage_result(
            stage_title,
            issues[stage_issue_start:],
            shape=shape,
            occ=occ,
            out_dir=out_dir,
            args=args,
            global_bbox=bbox,
        )
    issues.sort(
        key=lambda issue: {"high": 0, "medium": 1, "low": 2}.get(issue.severity, 3)
    )
    return issues, stats


def local_boss_cap_face(
    first: PlaneFace,
    second: PlaneFace,
    plane_by_index: dict[int, PlaneFace],
    adjacent_by_face: dict[int, set[int]],
    pull_dir: Vec3,
) -> PlaneFace | None:
    common = adjacent_by_face.get(first.face_index, set()) & adjacent_by_face.get(
        second.face_index, set()
    )
    pull = unit(pull_dir)
    candidates: list[PlaneFace] = []
    for face_index in common:
        face = plane_by_index.get(face_index)
        if face is None:
            continue
        if abs(dot(face.normal, pull)) < 0.75:
            continue
        if (
            max(
                abs(dot(face.normal, first.normal)),
                abs(dot(face.normal, second.normal)),
            )
            > 0.35
        ):
            continue
        candidates.append(face)
    if not candidates:
        return None
    return max(candidates, key=lambda face: face.area)


def build_wall_thickness_probe(
    shape: Any, occ: SimpleNamespace, args: argparse.Namespace
) -> dict[str, Any] | None:
    try:
        import vtk

        polydata = triangulate_shape(shape, occ, args.thickness_mesh_deflection_mm)
        if polydata.GetNumberOfCells() <= 0:
            return None
        tree = vtk.vtkOBBTree()
        tree.SetDataSet(polydata)
        tree.BuildLocator()
        bbox = shape_bbox(shape, occ)
        ray_length = bounds_diag(bbox) * 2.5
        min_hit_distance = max(args.thickness_min_hit_mm, args.model_tolerance_mm * 5.0)
        values: list[float] = []
        for center, normal, _area in iter_triangle_samples(
            polydata, args.thickness_samples
        ):
            thickness = estimate_thickness_at_point(
                tree, center, normal, ray_length, min_hit_distance
            )
            if thickness is not None and thickness > min_hit_distance:
                values.append(thickness)
        values.sort()
        nominal = percentile_from_sorted(values, 50.0)
        if nominal is None or nominal <= min_hit_distance:
            return None
        return {
            "tree": tree,
            "ray_length": ray_length,
            "min_hit_distance": min_hit_distance,
            "nominal_thickness_mm": nominal,
            "p25_thickness_mm": percentile_from_sorted(values, 25.0),
            "p75_thickness_mm": percentile_from_sorted(values, 75.0),
            "sample_count": len(values),
        }
    except Exception:
        return None


def thickness_hit_from_surface(
    tree: Any,
    point: Vec3,
    normal: Vec3,
    ray_length: float,
    min_hit_distance: float,
) -> dict[str, Any] | None:
    normal = unit(normal)
    candidates: list[dict[str, Any]] = []
    for direction in (normal, mul(normal, -1.0)):
        hit_distance = first_hit_distance_along(
            tree, point, direction, ray_length, min_hit_distance
        )
        if hit_distance is None:
            continue
        candidates.append({
            "thickness_mm": hit_distance,
            "direction": direction,
            "start": point,
            "end": add(point, mul(direction, hit_distance)),
        })
    if not candidates:
        return None
    return min(candidates, key=lambda item: float(item["thickness_mm"]))


def face_long_span_and_width(face: FaceInfo) -> tuple[float, float, float]:
    dims = [dim for dim in bbox_dimensions(face.bbox) if dim > 1e-6]
    if not dims:
        return 0.0, 0.0, 0.0
    long_span = max(dims)
    inferred_width = face.area / long_span if long_span > 1e-9 else 0.0
    aspect = long_span / inferred_width if inferred_width > 1e-9 else float("inf")
    return long_span, inferred_width, aspect


def cylinder_faces(face_infos: list[FaceInfo]) -> list[FaceInfo]:
    return [
        face
        for face in face_infos
        if face.kind == "cylinder"
        and face.axis is not None
        and face.radius is not None
        and face.radius > 1e-9
    ]


def cylinder_gap(first: FaceInfo, second: FaceInfo) -> tuple[float, Vec3] | None:
    if (
        first.axis is None
        or second.axis is None
        or first.radius is None
        or second.radius is None
    ):
        return None
    first_axis = unit(first.axis)
    second_axis = unit(second.axis)
    if abs(dot(first_axis, second_axis)) < 0.96:
        return None
    delta = sub(second.center, first.center)
    perpendicular = sub(delta, mul(first_axis, dot(delta, first_axis)))
    center_distance = norm(perpendicular)
    if center_distance <= 1e-9:
        return None
    direction = unit(perpendicular)
    gap = center_distance - first.radius - second.radius
    return gap, direction


def is_hole_like_cylinder(face: FaceInfo, max_diameter_mm: float) -> bool:
    if face.axis is None or face.radius is None:
        return False
    diameter = face.radius * 2.0
    return max_diameter_mm <= 0 or diameter <= max_diameter_mm


def cylinder_axis_center(face: FaceInfo) -> Vec3:
    if face.axis is None or face.axis_point is None:
        return face.center
    axis = unit(face.axis)
    return add(face.axis_point, mul(axis, dot(sub(face.center, face.axis_point), axis)))


def shape_to_edge_index(edge_shape: Any, shape_edges: list[Any]) -> int | None:
    for index, candidate in enumerate(shape_edges, start=1):
        try:
            if edge_shape.IsSame(candidate):
                return index
        except Exception:
            continue
    return None


def wire_edges(wire: Any, occ: SimpleNamespace) -> list[Any]:
    explorer = occ.BRepTools_WireExplorer(wire)
    edges = []
    while explorer.More():
        edges.append(occ.topods.Edge(explorer.Current()))
        explorer.Next()
    return edges


def edge_is_in_list(edge: Any, edges: list[Any]) -> bool:
    for candidate in edges:
        try:
            if edge.IsSame(candidate):
                return True
        except Exception:
            continue
    return False


def adjacent_face_indices_for_edge(
    edge: Any, edge_face_map: Any, shape_faces: list[Any], occ: SimpleNamespace
) -> list[int]:
    try:
        face_list = edge_face_map.FindFromKey(edge)
    except Exception:
        face_list = None
        for edge_map_index in range(1, edge_face_map.Size() + 1):
            try:
                if edge.IsSame(edge_face_map.FindKey(edge_map_index)):
                    face_list = edge_face_map.FindFromIndex(edge_map_index)
                    break
            except Exception:
                continue
    if face_list is None:
        return []

    indices: list[int] = []
    iterator = occ.TopTools_ListIteratorOfListOfShape(face_list)
    while iterator.More():
        face_index = shape_to_face_index(iterator.Value(), shape_faces)
        if face_index is not None:
            indices.append(face_index)
        iterator.Next()
    return sorted(set(indices))


def edge_exact_distance(
    first: Any, second: Any, occ: SimpleNamespace
) -> tuple[float, Vec3, Vec3] | None:
    try:
        distance_tool = occ.BRepExtrema_DistShapeShape(first, second)
        distance_tool.Perform()
        if distance_tool.IsDone() and distance_tool.NbSolution() > 0:
            return (
                float(distance_tool.Value()),
                gp_to_tuple(distance_tool.PointOnShape1(1)),
                gp_to_tuple(distance_tool.PointOnShape2(1)),
            )
    except Exception:
        pass

    first_points = sample_edge_points(first, occ, samples=48)
    second_points = sample_edge_points(second, occ, samples=48)
    best: tuple[float, Vec3, Vec3] | None = None
    for first_point in first_points:
        for second_point in second_points:
            value = distance(first_point, second_point)
            if best is None or value < best[0]:
                best = (value, first_point, second_point)
    return best


def cylinder_mouth_edge_clearances(
    cylinder: FaceInfo,
    shape_faces: list[Any],
    shape_edges: list[Any],
    edge_face_map: Any,
    info_by_index: dict[int, FaceInfo],
    occ: SimpleNamespace,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    if (
        cylinder.radius is None
        or cylinder.face_index <= 0
        or cylinder.face_index > len(shape_faces)
    ):
        return []

    cylinder_face = shape_faces[cylinder.face_index - 1]
    results: list[dict[str, Any]] = []
    radius_tolerance = max(args.model_tolerance_mm * 5.0, cylinder.radius * 0.02)

    for raw_edge in iter_shapes(cylinder_face, occ.TopAbs_EDGE, occ):
        hole_edge = occ.topods.Edge(raw_edge)
        try:
            curve = occ.BRepAdaptor_Curve(hole_edge)
            if curve.GetType() != occ.GeomAbs_Circle:
                continue
            hole_radius = float(curve.Circle().Radius())
        except Exception:
            continue
        if abs(hole_radius - cylinder.radius) > radius_tolerance:
            continue

        adjacent_indices = adjacent_face_indices_for_edge(
            hole_edge, edge_face_map, shape_faces, occ
        )
        mouth_face_indices = [
            index
            for index in adjacent_indices
            if index != cylinder.face_index
            and info_by_index.get(index) is not None
            and info_by_index[index].kind == "plane"
        ]
        hole_edge_index = shape_to_edge_index(hole_edge, shape_edges)

        for mouth_face_index in mouth_face_indices:
            mouth_face = shape_faces[mouth_face_index - 1]
            try:
                outer_edges = wire_edges(occ.breptools.OuterWire(mouth_face), occ)
            except Exception:
                continue
            if not outer_edges or edge_is_in_list(hole_edge, outer_edges):
                continue

            best: dict[str, Any] | None = None
            for outer_edge in outer_edges:
                distance_info = edge_exact_distance(hole_edge, outer_edge, occ)
                if distance_info is None:
                    continue
                clearance, hole_point, outer_point = distance_info
                candidate = {
                    "clearance_mm": clearance,
                    "hole_point": hole_point,
                    "outer_point": outer_point,
                    "mouth_face": mouth_face_index,
                    "hole_edge": hole_edge_index,
                    "outer_edge": shape_to_edge_index(outer_edge, shape_edges),
                }
                if best is None or candidate["clearance_mm"] < best["clearance_mm"]:
                    best = candidate
            if best is not None:
                results.append(best)

    return results


def safe_acos_deg(value: float) -> float:
    return math.degrees(math.acos(max(-1.0, min(1.0, value))))


def vtk_intersections(tree: Any, start: Vec3, end: Vec3) -> list[Vec3]:
    import vtk

    points = vtk.vtkPoints()
    ids = vtk.vtkIdList()
    tree.IntersectWithLine(start, end, points, ids)
    return [
        tuple(float(v) for v in points.GetPoint(index))
        for index in range(points.GetNumberOfPoints())
    ]


def estimate_thickness_at_point(
    tree: Any,
    point: Vec3,
    normal: Vec3,
    ray_length: float,
    min_hit_distance: float,
) -> float | None:
    normal = unit(normal)
    hits: list[float] = []
    for direction in (normal, mul(normal, -1.0)):
        start = add(point, mul(direction, min_hit_distance * 2.0))
        end = add(point, mul(direction, ray_length))
        for hit in vtk_intersections(tree, start, end):
            hit_distance = distance(point, hit)
            if hit_distance > min_hit_distance:
                hits.append(hit_distance)
    if not hits:
        return None
    return min(hits)


def iter_triangle_samples(
    polydata: Any, max_samples: int
) -> Iterable[tuple[Vec3, Vec3, float]]:
    cell_count = polydata.GetNumberOfCells()
    if cell_count <= 0:
        return
    step = max(1, math.ceil(cell_count / max(1, max_samples)))
    for cell_id in range(0, cell_count, step):
        cell = polydata.GetCell(cell_id)
        if cell is None or cell.GetNumberOfPoints() < 3:
            continue
        points = [
            tuple(float(v) for v in polydata.GetPoint(cell.GetPointId(i)))
            for i in range(3)
        ]
        edge_a = sub(points[1], points[0])
        edge_b = sub(points[2], points[0])
        normal = unit(cross(edge_a, edge_b))
        area = norm(cross(edge_a, edge_b)) / 2.0
        if area <= 1e-9:
            continue
        center = mul(add(add(points[0], points[1]), points[2]), 1.0 / 3.0)
        yield center, normal, area


def spatially_distinct_samples(
    samples: list[dict[str, Any]], min_distance_mm: float
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for sample in samples:
        point = sample["point"]
        if all(
            distance(point, existing["point"]) >= min_distance_mm
            for existing in selected
        ):
            selected.append(sample)
    return selected


def shape_to_face_index(face_shape: Any, shape_faces: list[Any]) -> int | None:
    for index, candidate in enumerate(shape_faces, start=1):
        try:
            if face_shape.IsSame(candidate):
                return index
        except Exception:
            continue
    return None


def adjacent_face_pairs(
    shape: Any, occ: SimpleNamespace, face_infos: list[FaceInfo]
) -> list[tuple[int, int, int]]:
    shape_faces = [
        occ.topods.Face(raw_face)
        for raw_face in iter_shapes(shape, occ.TopAbs_FACE, occ)
    ]
    edge_face_map = occ.TopTools_IndexedDataMapOfShapeListOfShape()
    occ.topexp.MapShapesAndAncestors(
        shape, occ.TopAbs_EDGE, occ.TopAbs_FACE, edge_face_map
    )
    pairs: set[tuple[int, int, int]] = set()
    for edge_map_index in range(1, edge_face_map.Size() + 1):
        face_list = edge_face_map.FindFromIndex(edge_map_index)
        iterator = occ.TopTools_ListIteratorOfListOfShape(face_list)
        face_indices: list[int] = []
        while iterator.More():
            face_index = shape_to_face_index(iterator.Value(), shape_faces)
            if face_index is not None:
                face_indices.append(face_index)
            iterator.Next()
        unique = sorted(set(face_indices))
        for first_index, first in enumerate(unique):
            for second in unique[first_index + 1 :]:
                pairs.add((first, second, edge_map_index))
    return sorted(pairs)


def face_info_by_index(face_infos: list[FaceInfo]) -> dict[int, FaceInfo]:
    return {face.face_index: face for face in face_infos}


def continuity_pair_is_relevant(
    first: FaceInfo,
    second: FaceInfo,
    include_plane_plane: bool,
    include_plane_cylinder: bool,
    include_plane_other: bool,
    include_cylinder_other: bool,
) -> tuple[bool, str | None]:
    kinds = {first.kind, second.kind}
    if kinds == {"plane"}:
        return (True, None) if include_plane_plane else (False, "plane_plane")
    if kinds == {"plane", "cylinder"}:
        return (True, None) if include_plane_cylinder else (False, "plane_cylinder")
    if "plane" in kinds:
        return (True, None) if include_plane_other else (False, "plane_other")
    if "cylinder" in kinds and kinds != {"cylinder"}:
        return (True, None) if include_cylinder_other else (False, "cylinder_other")
    return True, None


def first_hit_distance_along(
    tree: Any,
    point: Vec3,
    direction: Vec3,
    ray_length: float,
    min_hit_distance: float,
) -> float | None:
    direction = unit(direction)
    start = add(point, mul(direction, min_hit_distance))
    end = add(point, mul(direction, ray_length))
    distances = [
        distance(point, hit)
        for hit in vtk_intersections(tree, start, end)
        if distance(point, hit) > min_hit_distance
    ]
    return min(distances) if distances else None


def choose_release_direction_by_visibility(
    tree: Any,
    point: Vec3,
    pull_dir: Vec3,
    ray_length: float,
    min_hit_distance: float,
) -> tuple[Vec3, float | None, float | None]:
    positive = first_hit_distance_along(
        tree, point, pull_dir, ray_length, min_hit_distance
    )
    negative_dir = mul(pull_dir, -1.0)
    negative = first_hit_distance_along(
        tree, point, negative_dir, ray_length, min_hit_distance
    )
    positive_score = positive if positive is not None else float("inf")
    negative_score = negative if negative is not None else float("inf")
    if negative_score > positive_score:
        return negative_dir, negative, positive
    return pull_dir, positive, negative


def is_pocket_cap_face(
    face: FaceInfo, adjacent: FaceInfo, pull_dir: Vec3, args: argparse.Namespace
) -> bool:
    if face.normal is None or adjacent.normal is None:
        return False
    cap_alignment = abs(dot(face.normal, pull_dir))
    if cap_alignment < math.cos(math.radians(30.0)):
        return False
    wall_alignment = abs(dot(adjacent.normal, pull_dir))
    if wall_alignment > math.cos(math.radians(25.0)):
        return False
    into_cap_normal = dot(face.normal, sub(adjacent.center, face.center))
    return into_cap_normal > max(
        args.model_tolerance_mm * 5.0, bbox_extent_along_dir(face.bbox, pull_dir) * 0.1
    )


def face_boundary_metrics(face_shape: Any, occ: SimpleNamespace) -> dict[str, Any]:
    perimeter = 0.0
    edge_count = 0
    circular_edge_count = 0
    for raw_edge in iter_shapes(face_shape, occ.TopAbs_EDGE, occ):
        edge = occ.topods.Edge(raw_edge)
        try:
            length, _center = edge_props(edge, occ)
        except Exception:
            continue
        perimeter += length
        edge_count += 1
        try:
            curve = occ.BRepAdaptor_Curve(edge)
            if curve.GetType() == occ.GeomAbs_Circle:
                circular_edge_count += 1
        except Exception:
            pass
    return {
        "perimeter_mm": perimeter,
        "edge_count": edge_count,
        "circular_edge_count": circular_edge_count,
    }


def is_round_hole_cap_boundary(face: FaceInfo, metrics: dict[str, Any] | None) -> bool:
    if metrics is None:
        return False
    perimeter = float(metrics.get("perimeter_mm") or 0.0)
    edge_count = int(metrics.get("edge_count") or 0)
    if perimeter <= 1e-9 or edge_count <= 0:
        return False
    circularity = 4.0 * math.pi * face.area / (perimeter * perimeter)
    metrics["circularity"] = circularity
    return edge_count <= 2 and circularity >= 0.90


def analyze_hole_pocket_draft(
    face: FaceInfo,
    adjacent_infos: list[FaceInfo],
    boundary_metrics: dict[str, Any] | None,
    pull_dir: Vec3,
    tree: Any,
    ray_length: float,
    min_hit_distance: float,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    if face.normal is None:
        return None
    if not is_round_hole_cap_boundary(face, boundary_metrics):
        return None

    pocket_walls = [
        adjacent
        for adjacent in adjacent_infos
        if is_pocket_cap_face(face, adjacent, pull_dir, args)
    ]
    if not pocket_walls:
        return None

    release_dir, opening_hit, opposite_hit = choose_release_direction_by_visibility(
        tree,
        face.center,
        pull_dir,
        ray_length,
        min_hit_distance,
    )
    reverse_threshold = -math.sin(
        math.radians(max(args.undercut_negative_draft_deg, args.angle_tolerance_deg))
    )
    wall_checks: list[dict[str, Any]] = []
    for wall in pocket_walls:
        assert wall.normal is not None
        projection = dot(wall.normal, release_dir)
        signed_draft = math.degrees(math.asin(max(-1.0, min(1.0, projection))))
        if projection < reverse_threshold:
            wall_checks.append({
                "face": wall.face_index,
                "wall_projection": projection,
                "reverse_draft_deg": abs(signed_draft),
                "wall_kind": wall.kind,
            })

    return {
        "release_dir": release_dir,
        "opening_first_hit_mm": opening_hit,
        "opposite_first_hit_mm": opposite_hit,
        "cap_boundary": boundary_metrics,
        "pocket_wall_faces": [wall.face_index for wall in pocket_walls],
        "bad_walls": wall_checks,
    }


def triangulate_shape(shape: Any, occ: SimpleNamespace, deflection: float) -> Any:
    occ.BRepMesh_IncrementalMesh(shape, deflection)

    import vtk

    points = vtk.vtkPoints()
    polys = vtk.vtkCellArray()

    for raw_face in iter_shapes(shape, occ.TopAbs_FACE, occ):
        face = occ.topods.Face(raw_face)
        loc = occ.TopLoc_Location()
        triangulation = occ.BRep_Tool.Triangulation(face, loc)
        if triangulation is None:
            continue

        trsf = loc.Transformation()
        offset = points.GetNumberOfPoints()
        for node_index in range(1, triangulation.NbNodes() + 1):
            point = triangulation.Node(node_index)
            if not loc.IsIdentity():
                point = point.Transformed(trsf)
            points.InsertNextPoint(point.X(), point.Y(), point.Z())

        for tri_index in range(1, triangulation.NbTriangles() + 1):
            ids = list(triangulation.Triangle(tri_index).Get())
            if face.Orientation() == occ.TopAbs_REVERSED:
                ids = [ids[0], ids[2], ids[1]]
            triangle = vtk.vtkTriangle()
            triangle.GetPointIds().SetId(0, offset + ids[0] - 1)
            triangle.GetPointIds().SetId(1, offset + ids[1] - 1)
            triangle.GetPointIds().SetId(2, offset + ids[2] - 1)
            polys.InsertNextCell(triangle)

    polydata = vtk.vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetPolys(polys)
    return polydata


def render_with_vtk(
    shape: Any,
    occ: SimpleNamespace,
    issues: list[Issue],
    out_dir: Path,
    width: int,
    height: int,
    deflection: float,
) -> dict[str, ScreenAnnotation]:
    import vtk

    polydata = triangulate_shape(shape, occ, deflection)
    if polydata.GetNumberOfPoints() == 0:
        raise RuntimeError("OpenCascade meshing produced no display triangles.")

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(polydata)
    normals.ConsistencyOn()
    normals.SplittingOff()
    normals.Update()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(normals.GetOutputPort())

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(0.76, 0.0, 1.0)
    actor.GetProperty().SetSpecular(0.25)
    actor.GetProperty().SetSpecularPower(30)
    actor.GetProperty().EdgeVisibilityOn()
    actor.GetProperty().SetEdgeColor(0.22, 0.02, 0.35)
    actor.GetProperty().SetLineWidth(1.0)

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(1.0, 1.0, 1.0)
    renderer.AddActor(actor)

    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
    render_window.SetSize(width, height)
    render_window.AddRenderer(renderer)

    bounds = polydata.GetBounds()
    center = (
        (bounds[0] + bounds[1]) / 2.0,
        (bounds[2] + bounds[3]) / 2.0,
        (bounds[4] + bounds[5]) / 2.0,
    )
    diag = max(
        distance((bounds[0], bounds[2], bounds[4]), (bounds[1], bounds[3], bounds[5])),
        1.0,
    )
    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(*center)
    camera.SetPosition(
        center[0] + diag * 0.95, center[1] - diag * 1.25, center[2] + diag * 0.75
    )
    camera.SetViewUp(0.0, 0.0, 1.0)
    renderer.ResetCamera()
    camera.Zoom(1.2)
    render_window.Render()

    global_bbox = shape_bbox(shape, occ)
    global_diag = bounds_diag(global_bbox)
    screen_annotations: dict[str, ScreenAnnotation] = {}
    for issue in issues:
        ref_shapes = ref_shapes_for_issue(shape, occ, issue)
        ref_bounds = [
            (ref, shape_bbox(ref_shape, occ)) for ref, ref_shape in ref_shapes
        ]
        annotation_point = issue_annotation_world_point(shape, occ, issue, ref_shapes)
        local_bbox = union_bounds([bbox for _ref, bbox in ref_bounds]) or global_bbox
        target = annotation_point or issue.anchor or bbox_center(local_bbox)
        evidence_bbox = issue_evidence_bbox(
            issue,
            ref_bounds,
            global_diag,
            target,
            max(bounds_diag(local_bbox), global_diag * 0.035),
        )
        if target is None and evidence_bbox is None:
            continue
        fallback_world = target or bbox_center(evidence_bbox)  # type: ignore[arg-type]
        fallback_point = project_world_point(renderer, width, height, fallback_world)
        target_rect = (
            projected_bbox_rect(renderer, width, height, evidence_bbox)
            if evidence_bbox is not None
            else None
        )
        annotation = build_screen_annotation(
            issue, fallback_point, width, height, target_rect
        )
        screen_annotations[issue.id] = annotation
        store_render_check(issue, "overview", annotation)

    window_to_image = vtk.vtkWindowToImageFilter()
    window_to_image.SetInput(render_window)
    window_to_image.Update()

    base_path = out_dir / "model.png"
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(base_path))
    writer.SetInputConnection(window_to_image.GetOutputPort())
    writer.Write()
    emit_artifact_event(base_path)
    return screen_annotations


def get_ref_shape(shape: Any, occ: SimpleNamespace, ref: dict[str, int]) -> Any | None:
    kind = ref.get("kind")
    index = int(ref.get("index", 0))
    if index <= 0:
        return None

    if kind == "face":
        for current_index, raw_face in enumerate(
            iter_shapes(shape, occ.TopAbs_FACE, occ), start=1
        ):
            if current_index == index:
                return occ.topods.Face(raw_face)
    elif kind == "edge":
        for current_index, raw_edge in enumerate(
            iter_shapes(shape, occ.TopAbs_EDGE, occ), start=1
        ):
            if current_index == index:
                return occ.topods.Edge(raw_edge)
    return None


def union_bounds(bounds: list[BBox]) -> BBox | None:
    if not bounds:
        return None
    return (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        min(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
        max(bound[4] for bound in bounds),
        max(bound[5] for bound in bounds),
    )


def bounds_diag(bbox: BBox) -> float:
    return max(distance((bbox[0], bbox[1], bbox[2]), (bbox[3], bbox[4], bbox[5])), 1e-6)


def expand_bbox_around(point: Vec3, margin: float) -> BBox:
    return (
        point[0] - margin,
        point[1] - margin,
        point[2] - margin,
        point[0] + margin,
        point[1] + margin,
        point[2] + margin,
    )


def marker_radius_for_issue(issue: Issue, global_diag: float) -> float:
    if issue.code in {"small_face", "sliver_face"}:
        return max(global_diag * 0.003, 0.05)
    if issue.code == "side_action_cylinder" and "radius_mm" in issue.metric:
        return max(
            min(float(issue.metric["radius_mm"]) * 0.45, global_diag * 0.006), 0.15
        )
    return max(global_diag * 0.012, 0.8)


def show_3d_marker_for_issue(issue: Issue) -> bool:
    return False


def make_poly_actor(
    polydata: Any, color: tuple[float, float, float], opacity: float, edge: bool
) -> Any | None:
    if polydata.GetNumberOfPoints() == 0:
        return None

    import vtk

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(polydata)
    normals.ConsistencyOn()
    normals.SplittingOff()
    normals.Update()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(normals.GetOutputPort())
    try:
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
    except Exception:
        pass

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*color)
    actor.GetProperty().SetOpacity(opacity)
    if edge:
        actor.GetProperty().EdgeVisibilityOn()
        actor.GetProperty().SetEdgeColor(0.20, 0.02, 0.30)
        actor.GetProperty().SetLineWidth(1.0)
    return actor


def make_edge_tube_actor(edge: Any, occ: SimpleNamespace, radius: float) -> Any | None:
    import vtk

    samples = sample_edge_points(edge, occ, samples=96)
    if len(samples) < 2:
        return None

    points = vtk.vtkPoints()
    line = vtk.vtkPolyLine()
    line.GetPointIds().SetNumberOfIds(len(samples))
    for index, point in enumerate(samples):
        points.InsertNextPoint(*point)
        line.GetPointIds().SetId(index, index)

    cells = vtk.vtkCellArray()
    cells.InsertNextCell(line)
    polydata = vtk.vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetLines(cells)

    tube = vtk.vtkTubeFilter()
    tube.SetInputData(polydata)
    tube.SetRadius(radius)
    tube.SetNumberOfSides(18)
    tube.CappingOn()
    tube.Update()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(tube.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(1.0, 0.02, 0.02)
    actor.GetProperty().SetOpacity(1.0)
    return actor


def make_marker_actor(anchor: Vec3, radius: float) -> Any:
    import vtk

    sphere = vtk.vtkSphereSource()
    sphere.SetCenter(*anchor)
    sphere.SetRadius(radius)
    sphere.SetThetaResolution(32)
    sphere.SetPhiResolution(16)

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(sphere.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(1.0, 0.02, 0.02)
    actor.GetProperty().SetOpacity(1.0)
    return actor


def write_vtk_png(render_window: Any, path: Path) -> None:
    import vtk

    window_to_image = vtk.vtkWindowToImageFilter()
    window_to_image.SetInput(render_window)
    window_to_image.Update()

    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(path))
    writer.SetInputConnection(window_to_image.GetOutputPort())
    writer.Write()


def project_world_point(
    renderer: Any, width: int, height: int, point: Vec3
) -> tuple[int, int]:
    import vtk

    coordinate = vtk.vtkCoordinate()
    coordinate.SetCoordinateSystemToWorld()
    coordinate.SetValue(*point)
    x_display, y_display = coordinate.GetComputedDisplayValue(renderer)
    return (int(x_display), int(height - y_display))


def bbox_corners(bbox: BBox) -> list[Vec3]:
    return [
        (x, y, z)
        for x in (bbox[0], bbox[3])
        for y in (bbox[1], bbox[4])
        for z in (bbox[2], bbox[5])
    ]


def projected_bbox_rect_with(projector: Any, bbox: BBox) -> ScreenBBox:
    projected = [projector(point) for point in bbox_corners(bbox)]
    min_x = min(point[0] for point in projected)
    max_x = max(point[0] for point in projected)
    min_y = min(point[1] for point in projected)
    max_y = max(point[1] for point in projected)
    return (int(min_x), int(min_y), int(max_x), int(max_y))


def projected_bbox_rect(
    renderer: Any, width: int, height: int, bbox: BBox
) -> ScreenBBox:
    return projected_bbox_rect_with(
        lambda point: project_world_point(renderer, width, height, point), bbox
    )


def projected_bbox_radius(renderer: Any, width: int, height: int, bbox: BBox) -> int:
    rect = projected_bbox_rect(renderer, width, height, bbox)
    return int(max(rect[2] - rect[0], rect[3] - rect[1]) / 2.0)


def screen_bbox_center(rect: ScreenBBox) -> tuple[int, int]:
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def point_inside_screen_bbox(
    point: tuple[int, int], rect: ScreenBBox, padding: float = 0.0
) -> bool:
    return (
        rect[0] - padding <= point[0] <= rect[2] + padding
        and rect[1] - padding <= point[1] <= rect[3] + padding
    )


def annotation_radius_for_issue(
    issue: Issue, rect: ScreenBBox | None, default: int = 42
) -> int:
    if rect is None:
        return default
    span = max(rect[2] - rect[0], rect[3] - rect[1])
    if issue.code == "hole_draft_undercut":
        return max(34, min(72, int(span / 2.0) + 22))
    if issue.code in {
        "narrow_machining_gap",
        "thin_wall",
        "narrow_slot",
        "planar_step",
        "local_boss_thick",
    }:
        return max(36, min(78, int(span / 2.0) + 10))
    if issue.code in {
        "small_cylindrical_feature",
        "small_tool_radius",
        "small_circular_edge",
        "small_face",
        "sliver_face",
        "hole_edge_clearance",
        "hole_web_thin",
        "deep_hole_ratio",
    }:
        return max(42, min(135, int(span / 2.0) + 14))
    return max(34, min(100, int(span / 2.0) + 14))


def clamp_screen_point(
    point: tuple[int, int], width: int, height: int, margin: int = 20
) -> tuple[int, int]:
    return (
        clamp(point[0], margin, width - margin),
        clamp(point[1], margin, height - margin),
    )


def build_screen_annotation(
    issue: Issue,
    fallback_point: tuple[int, int],
    width: int,
    height: int,
    target_rect: ScreenBBox | None,
) -> ScreenAnnotation:
    point = fallback_point
    adjusted = False
    radius = annotation_radius_for_issue(issue, target_rect)
    if target_rect is not None:
        tolerance = max(16.0, radius * 0.55)
        if not point_inside_screen_bbox(point, target_rect, tolerance):
            point = screen_bbox_center(target_rect)
            adjusted = True
    clamped = clamp_screen_point(point, width, height)
    if clamped != point:
        adjusted = True
    return ScreenAnnotation(
        point=clamped, radius=radius, target_bbox=target_rect, adjusted=adjusted
    )


def metric_vec3(value: Any) -> Vec3 | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        vector = (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    return vector


def points_bbox(points: list[Vec3], margin: float = 0.0) -> BBox | None:
    if not points:
        return None
    return (
        min(point[0] for point in points) - margin,
        min(point[1] for point in points) - margin,
        min(point[2] for point in points) - margin,
        max(point[0] for point in points) + margin,
        max(point[1] for point in points) + margin,
        max(point[2] for point in points) + margin,
    )


def issue_metric_points(
    issue: Issue, target: Vec3 | None, local_diag: float
) -> list[Vec3]:
    points: list[Vec3] = []
    for key in (
        "measurement_start",
        "measurement_end",
        "hole_point",
        "outer_point",
        "point",
        "min_point",
        "max_point",
    ):
        point = metric_vec3(issue.metric.get(key))
        if point is not None:
            points.append(point)

    if (
        target is not None
        and issue.code in {"thin_wall_field", "thick_section"}
        and "thickness_mm" in issue.metric
    ):
        normal = metric_vec3(issue.metric.get("normal")) or unit(
            issue.view_dir or (1.0, 0.0, 0.0)
        )
        thickness = max(float(issue.metric.get("thickness_mm", 0.0)), 0.0)
        half = max(thickness / 2.0, local_diag * 0.015)
        points.extend([sub(target, mul(normal, half)), add(target, mul(normal, half))])

    if (
        target is not None
        and issue.code == "hole_web_thin"
        and "web_mm" in issue.metric
    ):
        direction = metric_vec3(issue.metric.get("direction")) or unit(
            issue.view_dir or (1.0, 0.0, 0.0)
        )
        half = max(float(issue.metric.get("web_mm", 0.0)) / 2.0, local_diag * 0.02)
        points.extend([
            sub(target, mul(direction, half)),
            add(target, mul(direction, half)),
        ])

    if (
        target is not None
        and issue.code == "deep_hole_ratio"
        and "depth_mm" in issue.metric
    ):
        axis = metric_vec3(issue.metric.get("axis")) or unit(
            issue.view_dir or (0.0, 0.0, 1.0)
        )
        half = max(float(issue.metric.get("depth_mm", 0.0)) / 2.0, local_diag * 0.03)
        points.extend([sub(target, mul(axis, half)), add(target, mul(axis, half))])

    if not points and issue.anchor is not None:
        points.append(issue.anchor)
    return points


def issue_evidence_bbox(
    issue: Issue,
    ref_bounds: list[tuple[dict[str, int], BBox]],
    global_diag: float,
    target: Vec3 | None,
    local_diag: float,
) -> BBox | None:
    metric_points = issue_metric_points(issue, target, local_diag)
    metric_bbox = points_bbox(metric_points)
    if metric_bbox is not None:
        margin = max(bounds_diag(metric_bbox) * 0.20, global_diag * 0.003, 0.02)
        if "diameter_mm" in issue.metric:
            margin = max(margin, float(issue.metric["diameter_mm"]) * 0.55)
        elif "radius_mm" in issue.metric:
            margin = max(margin, float(issue.metric["radius_mm"]) * 1.10)
        return points_bbox(metric_points, margin)

    preferred = preferred_annotation_bbox(issue, ref_bounds)
    if preferred is not None:
        return preferred
    if issue.anchor is not None:
        return expand_bbox_around(
            issue.anchor, marker_radius_for_issue(issue, global_diag)
        )
    return union_bounds([bbox for _ref, bbox in ref_bounds])


def store_render_check(issue: Issue, mode: str, annotation: ScreenAnnotation) -> None:
    checks = issue.metric.setdefault("render_checks", {})
    checks[mode] = {
        "point_px": annotation.point,
        "radius_px": annotation.radius,
        "target_bbox_px": annotation.target_bbox,
        "adjusted": annotation.adjusted,
    }


def issue_file_stem(issue: Issue) -> str:
    return f"{issue.id}_{issue.code}".replace(" ", "_")


def perpendicular_vector(vector: Vec3) -> Vec3:
    base = unit(vector)
    candidate = cross(base, (0.0, 0.0, 1.0))
    if norm(candidate) <= 1e-6:
        candidate = cross(base, (0.0, 1.0, 0.0))
    return unit(candidate, (1.0, 0.0, 0.0))


def pull_dir_for_issue(issue: Issue) -> Vec3:
    raw = issue.metric.get("pull_dir", (0.0, 0.0, 1.0))
    try:
        return unit((float(raw[0]), float(raw[1]), float(raw[2])), (0.0, 0.0, 1.0))
    except Exception:
        return (0.0, 0.0, 1.0)


def issue_candidate_view_dirs(issue: Issue) -> list[Vec3]:
    if issue.code == "hole_draft_undercut":
        pull = pull_dir_for_issue(issue)
        tangent = perpendicular_vector(pull)
        bitangent = unit(cross(pull, tangent), (0.0, 1.0, 0.0))
        candidates = [
            pull,
            mul(pull, -1.0),
            unit(add(mul(pull, 0.96), mul(tangent, 0.28))),
            unit(add(mul(pull, 0.96), mul(tangent, -0.28))),
            unit(add(mul(pull, 0.96), mul(bitangent, 0.28))),
            unit(add(mul(pull, 0.96), mul(bitangent, -0.28))),
        ]
        unique: list[Vec3] = []
        for candidate in candidates:
            if all(abs(dot(candidate, existing)) < 0.999 for existing in unique):
                unique.append(candidate)
        return unique

    if issue.code in {
        "narrow_machining_gap",
        "undercut_negative_draft",
        "side_action_cylinder",
        "hole_draft_undercut",
    }:
        if issue.code == "undercut_negative_draft" and "release_dir" in issue.metric:
            pull = unit(tuple(float(value) for value in issue.metric["release_dir"]))
        else:
            pull = pull_dir_for_issue(issue)
        feature_dir = unit(issue.view_dir or (1.0, 0.0, 0.0))
        candidates = [
            pull,
            mul(pull, -1.0),
            unit(add(pull, mul(feature_dir, 0.28))),
            unit(add(pull, mul(feature_dir, -0.28))),
            unit(add(mul(pull, -1.0), mul(feature_dir, 0.28))),
            unit(add(mul(pull, -1.0), mul(feature_dir, -0.28))),
            feature_dir,
            mul(feature_dir, -1.0),
        ]
        unique: list[Vec3] = []
        for candidate in candidates:
            if all(abs(dot(candidate, existing)) < 0.999 for existing in unique):
                unique.append(candidate)
        return unique

    base = unit(issue.view_dir or (0.95, -1.25, 0.75))
    tangent = perpendicular_vector(base)
    bitangent = unit(cross(base, tangent), (0.0, 0.0, 1.0))
    candidates = [
        base,
        mul(base, -1.0),
        unit(add(base, mul(tangent, 0.35))),
        unit(add(base, mul(tangent, -0.35))),
        unit(add(base, mul(bitangent, 0.35))),
        unit(add(base, mul(bitangent, -0.35))),
    ]
    unique: list[Vec3] = []
    for candidate in candidates:
        if all(abs(dot(candidate, existing)) < 0.999 for existing in unique):
            unique.append(candidate)
    return unique


def oblique_view_dir(front_dir: Vec3) -> Vec3:
    tangent = perpendicular_vector(front_dir)
    bitangent = unit(cross(front_dir, tangent), (0.0, 0.0, 1.0))
    return unit(
        add(add(mul(front_dir, 0.82), mul(tangent, 0.38)), mul(bitangent, 0.28))
    )


def section_plane_normal(issue: Issue, camera_dir: Vec3) -> Vec3:
    base = unit(issue.view_dir or camera_dir)
    if issue.code in {
        "small_cylindrical_feature",
        "small_tool_radius",
        "small_circular_edge",
    }:
        return perpendicular_vector(base)
    if issue.code in {
        "small_face",
        "sliver_face",
        "local_boss_thick",
        "narrow_machining_gap",
        "hole_edge_clearance",
        "hole_web_thin",
        "deep_hole_ratio",
        "thin_wall",
        "thin_wall_field",
        "thick_section",
        "thickness_variation",
        "planar_step",
        "narrow_slot",
        "undercut_negative_draft",
        "side_action_cylinder",
        "hole_draft_undercut",
        "surface_g1_break",
        "surface_g2_jump",
    }:
        return perpendicular_vector(base)
    return camera_dir


def clipping_plane_for_issue(
    issue: Issue, camera_dir: Vec3, target: Vec3
) -> tuple[Vec3, Vec3]:
    return target, section_plane_normal(issue, camera_dir)


def ref_shapes_for_issue(
    shape: Any, occ: SimpleNamespace, issue: Issue
) -> list[tuple[dict[str, int], Any]]:
    refs = []
    for ref in issue.refs:
        ref_shape = get_ref_shape(shape, occ, ref)
        if ref_shape is not None:
            refs.append((ref, ref_shape))
    return refs


def ref_centers(
    ref_shapes: list[tuple[dict[str, int], Any]], occ: SimpleNamespace
) -> list[Vec3]:
    centers = []
    for _ref, ref_shape in ref_shapes:
        centers.append(bbox_center(shape_bbox(ref_shape, occ)))
    return centers


def ref_bounds_for_issue(
    shape: Any, occ: SimpleNamespace, issue: Issue
) -> list[tuple[dict[str, int], BBox]]:
    bounds = []
    for ref in issue.refs:
        ref_shape = get_ref_shape(shape, occ, ref)
        if ref_shape is not None:
            bounds.append((ref, shape_bbox(ref_shape, occ)))
    return bounds


def preferred_annotation_bbox(
    issue: Issue, ref_bounds: list[tuple[dict[str, int], BBox]]
) -> BBox | None:
    if not ref_bounds:
        return None
    if issue.code == "local_boss_thick" and "cap_face" in issue.metric:
        cap_face = int(issue.metric["cap_face"])
        for ref, bbox in ref_bounds:
            if ref.get("kind") == "face" and int(ref.get("index", 0)) == cap_face:
                return bbox
    if issue.code in {
        "narrow_machining_gap",
        "thin_wall",
        "narrow_slot",
        "planar_step",
    }:
        return min((bbox for _ref, bbox in ref_bounds), key=bounds_diag)
    return union_bounds([bbox for _ref, bbox in ref_bounds])


def issue_annotation_world_point(
    shape: Any,
    occ: SimpleNamespace,
    issue: Issue,
    ref_shapes: list[tuple[dict[str, int], Any]] | None = None,
) -> Vec3 | None:
    if (
        issue.code == "hole_edge_clearance"
        and "hole_point" in issue.metric
        and "outer_point" in issue.metric
    ):
        hole_point = tuple(float(value) for value in issue.metric["hole_point"])
        outer_point = tuple(float(value) for value in issue.metric["outer_point"])
        return mul(add(hole_point, outer_point), 0.5)

    ref_bounds: list[tuple[dict[str, int], BBox]] = []
    if ref_shapes is not None:
        ref_bounds = [
            (ref, shape_bbox(ref_shape, occ)) for ref, ref_shape in ref_shapes
        ]
    elif issue.refs:
        ref_bounds = ref_bounds_for_issue(shape, occ, issue)
    annotation_bbox = preferred_annotation_bbox(issue, ref_bounds)
    if annotation_bbox is not None and issue.code in {
        "narrow_machining_gap",
        "thin_wall",
        "narrow_slot",
        "planar_step",
        "local_boss_thick",
        "small_face",
        "sliver_face",
    }:
        return bbox_center(annotation_bbox)

    if issue.anchor is not None:
        return issue.anchor
    if annotation_bbox is not None:
        return bbox_center(annotation_bbox)
    return None


def ensure_min_screen_span(
    start: tuple[int, int],
    end: tuple[int, int],
    min_span: int = 90,
) -> tuple[tuple[int, int], tuple[int, int]]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length >= min_span or length <= 1e-6:
        return start, end
    scale = min_span / length
    cx = (start[0] + end[0]) / 2.0
    cy = (start[1] + end[1]) / 2.0
    half_dx = dx * scale / 2.0
    half_dy = dy * scale / 2.0
    return (int(cx - half_dx), int(cy - half_dy)), (
        int(cx + half_dx),
        int(cy + half_dy),
    )


def limit_screen_span(
    start: tuple[int, int],
    end: tuple[int, int],
    max_span: int,
) -> tuple[tuple[int, int], tuple[int, int]]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= max_span or length <= 1e-6:
        return start, end
    scale = max_span / length
    cx = (start[0] + end[0]) / 2.0
    cy = (start[1] + end[1]) / 2.0
    half_dx = dx * scale / 2.0
    half_dy = dy * scale / 2.0
    return (int(cx - half_dx), int(cy - half_dy)), (
        int(cx + half_dx),
        int(cy + half_dy),
    )


def build_issue_overlays(
    issue: Issue,
    renderer: Any,
    width: int,
    height: int,
    target: Vec3,
    local_diag: float,
    mode: str,
) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    red_text_offset = (18, -52)

    if (
        issue.code
        in {
            "thin_wall",
            "planar_step",
            "narrow_slot",
            "local_boss_thick",
            "narrow_machining_gap",
        }
        and "distance_mm" in issue.metric
    ):
        measurement_dir = issue.metric.get(
            "measurement_dir", issue.view_dir or (1.0, 0.0, 0.0)
        )
        normal = unit(tuple(float(value) for value in measurement_dir))
        distance_mm = float(issue.metric["distance_mm"])
        if "measurement_start" in issue.metric and "measurement_end" in issue.metric:
            start_world = tuple(
                float(value) for value in issue.metric["measurement_start"]
            )
            end_world = tuple(float(value) for value in issue.metric["measurement_end"])
        else:
            half = max(distance_mm / 2.0, local_diag * 0.015)
            start_world = sub(target, mul(normal, half))
            end_world = add(target, mul(normal, half))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        if issue.code == "thin_wall":
            label = f"壁厚 {distance_mm:.3f} mm"
        elif issue.code == "narrow_slot":
            label = f"槽宽 {distance_mm:.3f} mm"
        elif issue.code == "local_boss_thick":
            if "thickness_ratio" in issue.metric:
                label = f"局部厚度 {distance_mm:.3f} mm ({float(issue.metric['thickness_ratio']):.1f}x)"
            else:
                label = f"局部厚度 {distance_mm:.3f} mm"
        elif issue.code == "narrow_machining_gap":
            label = f"加工间隙 {distance_mm:.3f} mm"
        else:
            label = f"错位 {distance_mm:.3f} mm"
        overlays.append({"kind": "measure", "start": start, "end": end, "text": label})

    if (
        issue.code in {"thin_wall_field", "thick_section"}
        and "thickness_mm" in issue.metric
    ):
        normal = unit(
            tuple(issue.metric.get("normal", issue.view_dir or (1.0, 0.0, 0.0)))
        )
        thickness = float(issue.metric["thickness_mm"])
        half = max(thickness / 2.0, local_diag * 0.015)
        start_world = sub(target, mul(normal, half))
        end_world = add(target, mul(normal, half))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        overlays.append({
            "kind": "measure",
            "start": start,
            "end": end,
            "text": f"厚度 {thickness:.3f} mm",
        })

    if issue.code == "thickness_variation" and "ratio" in issue.metric:
        text = f"厚度比 {float(issue.metric['ratio']):.2f}"
        overlays.append({
            "kind": "line",
            "start": (24, 86),
            "end": (220, 86),
            "text": text,
            "label_at": (235, 68),
        })

    if issue.code == "small_face" and "area_mm2" in issue.metric:
        text = f"面积 {float(issue.metric['area_mm2']):.4f} mm²"
        overlays.append({
            "kind": "line",
            "start": (24, 86),
            "end": (220, 86),
            "text": text,
            "label_at": (235, 68),
        })

    if issue.code == "sliver_face" and "estimated_width_mm" in issue.metric:
        text = f"窄面宽 {float(issue.metric['estimated_width_mm']):.4f} mm"
        overlays.append({
            "kind": "line",
            "start": (24, 86),
            "end": (220, 86),
            "text": text,
            "label_at": (235, 68),
        })

    if issue.code == "hole_edge_clearance" and "clearance_mm" in issue.metric:
        clearance = max(float(issue.metric["clearance_mm"]), 0.0)
        if "hole_point" in issue.metric and "outer_point" in issue.metric:
            start_world = tuple(float(value) for value in issue.metric["hole_point"])
            end_world = tuple(float(value) for value in issue.metric["outer_point"])
        else:
            axis_index = int(issue.metric.get("clearance_axis", 0))
            sign = float(issue.metric.get("clearance_sign", 1.0))
            direction = principal_axis(axis_index, sign)
            radius = max(
                float(issue.metric.get("diameter_mm", 0.0)) / 2.0, local_diag * 0.015
            )
            start_world = add(target, mul(direction, radius))
            end_world = add(
                start_world, mul(direction, max(clearance, local_diag * 0.035))
            )
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        overlays.append({
            "kind": "measure",
            "start": start,
            "end": end,
            "text": f"边距 {clearance:.3f} mm",
        })

    if issue.code == "hole_web_thin" and "web_mm" in issue.metric:
        direction_raw = issue.metric.get("direction", issue.view_dir or (1.0, 0.0, 0.0))
        direction = unit((
            float(direction_raw[0]),
            float(direction_raw[1]),
            float(direction_raw[2]),
        ))
        web = max(float(issue.metric["web_mm"]), 0.0)
        half = max(web / 2.0, local_diag * 0.02)
        start_world = sub(target, mul(direction, half))
        end_world = add(target, mul(direction, half))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        overlays.append({
            "kind": "measure",
            "start": start,
            "end": end,
            "text": f"孔间净距 {web:.3f} mm",
        })

    if issue.code == "deep_hole_ratio" and "depth_mm" in issue.metric:
        axis_raw = issue.metric.get("axis", issue.view_dir or (0.0, 0.0, 1.0))
        axis = unit((float(axis_raw[0]), float(axis_raw[1]), float(axis_raw[2])))
        depth = float(issue.metric["depth_mm"])
        half = max(depth / 2.0, local_diag * 0.06)
        start_world = sub(target, mul(axis, half))
        end_world = add(target, mul(axis, half))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        ratio = float(issue.metric.get("depth_diameter_ratio", 0.0))
        overlays.append({
            "kind": "measure",
            "start": start,
            "end": end,
            "text": f"深度 {depth:.3f} mm / L/D {ratio:.2f}",
        })

    if issue.code == "low_draft":
        pull_dir = tuple(issue.metric.get("pull_dir", (0.0, 0.0, 1.0)))
        pull = unit((float(pull_dir[0]), float(pull_dir[1]), float(pull_dir[2])))
        span = max(local_diag * 0.55, 30.0)
        start_world = sub(target, mul(pull, span * 0.35))
        end_world = add(target, mul(pull, span * 0.35))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        angle = float(issue.metric.get("draft_angle_deg", 0.0))
        threshold = float(issue.metric.get("threshold_deg", 0.0))
        label_at = (
            min(width - 260, max(10, end[0] + red_text_offset[0])),
            min(height - 45, max(60, end[1] + red_text_offset[1])),
        )
        overlays.append({
            "kind": "arrow",
            "start": start,
            "end": end,
            "text": f"拔模方向 角度 {angle:.2f}° < {threshold:.2f}°",
            "label_at": label_at,
        })

    if issue.code in {"undercut_negative_draft", "side_action_cylinder"}:
        slider_dir_raw = issue.metric.get(
            "candidate_slider_dir", issue.view_dir or (1.0, 0.0, 0.0)
        )
        slider_dir = unit((
            float(slider_dir_raw[0]),
            float(slider_dir_raw[1]),
            float(slider_dir_raw[2]),
        ))
        span = max(local_diag * 0.60, 30.0)
        start_world = sub(target, mul(slider_dir, span * 0.35))
        end_world = add(target, mul(slider_dir, span * 0.35))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        start, end = limit_screen_span(start, end, 240)
        label_at = (
            min(width - 260, max(10, end[0] + 18)),
            min(height - 45, max(60, end[1] - 52)),
        )
        overlays.append({
            "kind": "arrow",
            "start": start,
            "end": end,
            "text": "候选滑块/抽芯方向",
            "label_at": label_at,
        })

    if issue.code in {"undercut_negative_draft", "hole_draft_undercut"}:
        direction_raw = issue.metric.get(
            "release_dir", issue.metric.get("pull_dir", (0.0, 0.0, 1.0))
        )
        pull_dir = unit((
            float(direction_raw[0]),
            float(direction_raw[1]),
            float(direction_raw[2]),
        ))
        span = max(local_diag * 0.55, 30.0)
        start_world = sub(target, mul(pull_dir, span * 0.35))
        end_world = add(target, mul(pull_dir, span * 0.35))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        start, end = limit_screen_span(start, end, 240)
        signed_angle = float(
            issue.metric.get(
                "worst_reverse_draft_deg"
                if issue.code == "hole_draft_undercut"
                else "signed_draft_deg",
                0.0,
            )
        )
        direction_label = (
            "可释放方向" if "release_dir" in issue.metric else "主拔模方向"
        )
        label_at = (
            min(width - 260, max(10, end[0] + 18)),
            min(height - 45, max(60, end[1] + 18)),
        )
        overlays.append({
            "kind": "arrow",
            "start": start,
            "end": end,
            "text": f"{direction_label} 反向拔模 {signed_angle:.2f}°",
            "label_at": label_at,
        })

    if issue.code in {"surface_g1_break", "surface_g2_jump"}:
        if "normal_angle_deg" in issue.metric:
            text = f"G1 夹角 {float(issue.metric['normal_angle_deg']):.2f}°"
        else:
            text = f"G2 曲率跳变 {float(issue.metric.get('curvature_jump', 0.0)):.4f}"
        overlays.append({
            "kind": "line",
            "start": (24, 86),
            "end": (220, 86),
            "text": text,
            "label_at": (235, 68),
        })

    if (
        issue.code in {"small_cylindrical_feature", "small_circular_edge"}
        and "diameter_mm" in issue.metric
    ):
        diameter = float(issue.metric["diameter_mm"])
        tangent = perpendicular_vector(issue.view_dir or (0.0, 0.0, 1.0))
        start_world = sub(target, mul(tangent, diameter / 2.0))
        end_world = add(target, mul(tangent, diameter / 2.0))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end)
        overlays.append({
            "kind": "measure",
            "start": start,
            "end": end,
            "text": f"直径 {diameter:.3f} mm",
        })

    if issue.code == "small_tool_radius" and "radius_mm" in issue.metric:
        radius = float(issue.metric["radius_mm"])
        tangent = perpendicular_vector(issue.view_dir or (0.0, 0.0, 1.0))
        start_world = target
        end_world = add(target, mul(tangent, radius))
        start = project_world_point(renderer, width, height, start_world)
        end = project_world_point(renderer, width, height, end_world)
        start, end = ensure_min_screen_span(start, end, min_span=70)
        overlays.append({
            "kind": "measure",
            "start": start,
            "end": end,
            "text": f"半径 {radius:.3f} mm",
        })

    if mode == "section":
        overlays.append({
            "kind": "line",
            "start": (24, height - 40),
            "end": (220, height - 40),
            "text": "局部剖视",
            "label_at": (235, height - 58),
        })
    return overlays


def bbox_exit_distance(origin: Vec3, direction: Vec3, bbox: BBox) -> float:
    direction = unit(direction)
    distances = []
    mins = (bbox[0], bbox[1], bbox[2])
    maxs = (bbox[3], bbox[4], bbox[5])
    for axis in range(3):
        component = direction[axis]
        if abs(component) <= 1e-9:
            continue
        plane = maxs[axis] if component > 0 else mins[axis]
        distance_to_plane = (plane - origin[axis]) / component
        if distance_to_plane > 0:
            distances.append(distance_to_plane)
    return min(distances) if distances else float("inf")


def choose_unobstructed_view_dir(issue: Issue, global_bbox: BBox, target: Vec3) -> Vec3:
    candidates = issue_candidate_view_dirs(issue)
    if not candidates:
        return view_dir_for_issue(issue)
    scored = []
    for candidate in candidates:
        exit_distance = bbox_exit_distance(target, candidate, global_bbox)
        alignment = abs(dot(unit(issue.view_dir or candidate), candidate))
        scored.append((exit_distance - alignment * 0.01, candidate))
    scored.sort(key=lambda item: item[0])
    return unit(scored[0][1])


def add_clipping_plane(actor: Any, origin: Vec3, normal: Vec3) -> None:
    import vtk

    plane = vtk.vtkPlane()
    plane.SetOrigin(*origin)
    plane.SetNormal(*normal)
    mapper = actor.GetMapper()
    if mapper is not None:
        mapper.AddClippingPlane(plane)


def camera_up_for(view_dir: Vec3) -> Vec3:
    z_up = (0.0, 0.0, 1.0)
    if abs(dot(unit(view_dir), z_up)) < 0.92:
        return z_up
    return (0.0, 1.0, 0.0)


def view_dir_for_issue(issue: Issue) -> Vec3:
    base = unit(issue.view_dir or (0.95, -1.25, 0.75))
    if abs(dot(base, (0.0, 0.0, 1.0))) > 0.88:
        return unit(add(mul(base, 1.0), (0.28, -0.18, 0.0)))
    return unit(add(mul(base, 1.0), (0.0, 0.0, 0.22)))


def render_single_issue_view(
    shape: Any,
    occ: SimpleNamespace,
    issue: Issue,
    out_dir: Path,
    width: int,
    height: int,
    deflection: float,
    global_bbox: BBox,
    mode: str,
    view_dir: Vec3,
    local_bbox: BBox,
    ref_shapes: list[tuple[dict[str, int], Any]],
) -> str:
    import vtk

    global_diag = bounds_diag(global_bbox)
    local_diag = max(bounds_diag(local_bbox), global_diag * 0.035)
    annotation_point = issue_annotation_world_point(shape, occ, issue, ref_shapes)
    target = annotation_point or issue.anchor or bbox_center(local_bbox)

    model_polydata = triangulate_shape(shape, occ, deflection)
    model_actor = make_poly_actor(model_polydata, (0.72, 0.0, 0.95), 0.72, edge=True)
    if model_actor is None:
        raise RuntimeError("OpenCascade meshing produced no display triangles.")

    highlight_actors = []
    tube_radius = max(global_diag * 0.0035, 0.35)

    for ref, ref_shape in ref_shapes:
        if ref.get("kind") == "face":
            actor = make_poly_actor(
                triangulate_shape(ref_shape, occ, max(deflection * 0.35, 0.05)),
                (1.0, 0.03, 0.02),
                0.96,
                edge=True,
            )
        elif ref.get("kind") == "edge":
            actor = make_edge_tube_actor(ref_shape, occ, tube_radius)
        else:
            actor = None
        if actor is not None:
            highlight_actors.append(actor)

    if (
        issue.anchor is not None
        and issue.code not in {"small_face", "sliver_face"}
        and show_3d_marker_for_issue(issue)
    ):
        marker_radius = marker_radius_for_issue(issue, global_diag)
        marker_actor = make_marker_actor(issue.anchor, marker_radius)
        highlight_actors.append(marker_actor)

    if mode == "section":
        origin, normal = clipping_plane_for_issue(issue, view_dir, target)
        add_clipping_plane(model_actor, origin, normal)
        for actor in highlight_actors:
            add_clipping_plane(actor, origin, normal)

    camera_distance = max(local_diag * 5.0, global_diag * 0.10)

    renderer = vtk.vtkRenderer()
    renderer.SetBackground(1.0, 1.0, 1.0)
    renderer.AddActor(model_actor)
    for actor in highlight_actors:
        renderer.AddActor(actor)

    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
    render_window.SetSize(width, height)
    render_window.AddRenderer(renderer)

    view_dir = unit(view_dir)
    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(*target)
    camera.SetPosition(*add(target, mul(view_dir, camera_distance)))
    camera.SetViewUp(*camera_up_for(view_dir))
    camera.SetViewAngle(24.0)
    renderer.ResetCameraClippingRange()
    render_window.Render()

    ref_bounds = [(ref, shape_bbox(ref_shape, occ)) for ref, ref_shape in ref_shapes]
    evidence_bbox = issue_evidence_bbox(
        issue, ref_bounds, global_diag, target, local_diag
    )
    fallback_world = annotation_point or target
    fallback_point = project_world_point(renderer, width, height, fallback_world)
    target_rect = (
        projected_bbox_rect(renderer, width, height, evidence_bbox)
        if evidence_bbox is not None
        else None
    )
    annotation = build_screen_annotation(
        issue, fallback_point, width, height, target_rect
    )
    store_render_check(issue, mode, annotation)

    overlays = build_issue_overlays(
        issue, renderer, width, height, target, local_diag, mode
    )
    stem = issue_file_stem(issue)
    raw_path = out_dir / f".{stem}_{mode}_raw.png"
    write_vtk_png(render_window, raw_path)
    final_name = f"{stem}_{mode}.png"
    final_path = out_dir / final_name
    title_suffix = {"front": "正视", "oblique": "斜视", "section": "剖视"}.get(
        mode, mode
    )
    annotate_png(
        raw_path,
        final_path,
        [(issue, annotation.point, annotation.radius)],
        f"{issue.id} {issue.title} - {title_suffix}",
        overlays=overlays,
    )
    raw_path.unlink(missing_ok=True)
    return final_name


def render_issue_with_vtk(
    shape: Any,
    occ: SimpleNamespace,
    issue: Issue,
    out_dir: Path,
    width: int,
    height: int,
    deflection: float,
    global_bbox: BBox,
) -> list[str]:
    global_diag = bounds_diag(global_bbox)
    ref_shapes = ref_shapes_for_issue(shape, occ, issue)
    ref_bounds = [shape_bbox(ref_shape, occ) for _ref, ref_shape in ref_shapes]
    annotation_point = issue_annotation_world_point(shape, occ, issue, ref_shapes)
    if issue.anchor is not None and issue.code not in {"small_face", "sliver_face"}:
        marker_radius = marker_radius_for_issue(issue, global_diag)
        ref_bounds.append((
            issue.anchor[0] - marker_radius,
            issue.anchor[1] - marker_radius,
            issue.anchor[2] - marker_radius,
            issue.anchor[0] + marker_radius,
            issue.anchor[1] + marker_radius,
            issue.anchor[2] + marker_radius,
        ))
    if annotation_point is not None and issue.code in {
        "narrow_machining_gap",
        "thin_wall",
        "narrow_slot",
        "planar_step",
        "local_boss_thick",
    }:
        marker_radius = marker_radius_for_issue(issue, global_diag)
        ref_bounds.append((
            annotation_point[0] - marker_radius,
            annotation_point[1] - marker_radius,
            annotation_point[2] - marker_radius,
            annotation_point[0] + marker_radius,
            annotation_point[1] + marker_radius,
            annotation_point[2] + marker_radius,
        ))
    local_bbox = union_bounds(ref_bounds) or global_bbox
    target = annotation_point or issue.anchor or bbox_center(local_bbox)
    if issue.code == "hole_draft_undercut" and issue.anchor is not None:
        diameter = float(
            issue.metric.get("diameter_mm")
            or issue.metric.get("estimated_diameter_mm")
            or 0.0
        )
        context_margin = max(diameter * 7.0, global_diag * 0.07)
        local_bbox = (
            union_bounds([local_bbox, expand_bbox_around(issue.anchor, context_margin)])
            or local_bbox
        )

    front_dir = choose_unobstructed_view_dir(issue, global_bbox, target)
    oblique_dir = oblique_view_dir(front_dir)
    section_dir = (
        oblique_dir
        if issue.code
        in {
            "small_cylindrical_feature",
            "small_tool_radius",
            "small_circular_edge",
            "small_face",
            "sliver_face",
            "local_boss_thick",
            "narrow_machining_gap",
            "hole_edge_clearance",
            "hole_web_thin",
            "deep_hole_ratio",
            "thin_wall",
            "thin_wall_field",
            "thick_section",
            "thickness_variation",
            "planar_step",
            "narrow_slot",
            "undercut_negative_draft",
            "side_action_cylinder",
            "hole_draft_undercut",
            "surface_g1_break",
            "surface_g2_jump",
        }
        else front_dir
    )

    images = []
    for mode, view_dir in (
        ("front", front_dir),
        ("oblique", oblique_dir),
        ("section", section_dir),
    ):
        try:
            images.append(
                render_single_issue_view(
                    shape,
                    occ,
                    issue,
                    out_dir,
                    width,
                    height,
                    deflection,
                    global_bbox,
                    mode,
                    view_dir,
                    local_bbox,
                    ref_shapes,
                )
            )
        except Exception as exc:
            print(
                f"[warn] targeted {mode} render failed for {issue.id}: {exc}",
                file=sys.stderr,
            )
    if not images:
        raise RuntimeError(f"all targeted renders failed for {issue.id}")
    return images


def sample_edge_points(
    edge: Any, occ: SimpleNamespace, samples: int = 24
) -> list[Vec3]:
    curve = occ.BRepAdaptor_Curve(edge)
    first = float(curve.FirstParameter())
    last = float(curve.LastParameter())
    if not (math.isfinite(first) and math.isfinite(last)) or abs(last - first) <= 1e-12:
        try:
            _, center = edge_props(edge, occ)
            return [center]
        except Exception:
            return []
    points = []
    for index in range(samples + 1):
        parameter = first + (last - first) * (index / samples)
        try:
            points.append(gp_to_tuple(curve.Value(parameter)))
        except Exception:
            pass
    return points


def projection_basis() -> tuple[Vec3, Vec3]:
    view = unit((0.95, -1.25, 0.75))
    up_hint = (0.0, 0.0, 1.0)
    right = unit(cross(view, up_hint), (1.0, 0.0, 0.0))
    up = unit(cross(right, view), (0.0, 1.0, 0.0))
    return right, up


def project_point(point: Vec3, right: Vec3, up: Vec3) -> tuple[float, float]:
    return (dot(point, right), dot(point, up))


def render_projection_pil(
    shape: Any,
    occ: SimpleNamespace,
    issues: list[Issue],
    out_dir: Path,
    width: int,
    height: int,
) -> dict[str, ScreenAnnotation]:
    from PIL import Image, ImageDraw

    edge_polylines: list[list[Vec3]] = []
    all_points: list[Vec3] = []
    for raw_edge in iter_shapes(shape, occ.TopAbs_EDGE, occ):
        edge = occ.topods.Edge(raw_edge)
        polyline = sample_edge_points(edge, occ)
        if len(polyline) >= 2:
            edge_polylines.append(polyline)
            all_points.extend(polyline)

    if not all_points:
        bbox = shape_bbox(shape, occ)
        all_points = [
            (bbox[0], bbox[1], bbox[2]),
            (bbox[3], bbox[4], bbox[5]),
        ]

    right, up = projection_basis()
    projected = [project_point(point, right, up) for point in all_points]
    min_x = min(point[0] for point in projected)
    max_x = max(point[0] for point in projected)
    min_y = min(point[1] for point in projected)
    max_y = max(point[1] for point in projected)
    pad = 50
    scale = min(
        (width - pad * 2) / max(max_x - min_x, 1e-9),
        (height - pad * 2) / max(max_y - min_y, 1e-9),
    )

    def to_screen(point: Vec3) -> tuple[int, int]:
        x, y = project_point(point, right, up)
        return (
            int((x - min_x) * scale + pad),
            int(height - ((y - min_y) * scale + pad)),
        )

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for polyline in edge_polylines:
        screen_line = [to_screen(point) for point in polyline]
        draw.line(screen_line, fill=(120, 0, 200), width=3)
        draw.line(screen_line, fill=(210, 0, 255), width=1)
    model_path = out_dir / "model.png"
    image.save(model_path)
    emit_artifact_event(model_path)

    global_bbox = shape_bbox(shape, occ)
    global_diag = bounds_diag(global_bbox)
    screen_annotations: dict[str, ScreenAnnotation] = {}
    for issue in issues:
        ref_shapes = ref_shapes_for_issue(shape, occ, issue)
        ref_bounds = [
            (ref, shape_bbox(ref_shape, occ)) for ref, ref_shape in ref_shapes
        ]
        annotation_point = issue_annotation_world_point(shape, occ, issue, ref_shapes)
        local_bbox = union_bounds([bbox for _ref, bbox in ref_bounds]) or global_bbox
        target = annotation_point or issue.anchor or bbox_center(local_bbox)
        evidence_bbox = issue_evidence_bbox(
            issue,
            ref_bounds,
            global_diag,
            target,
            max(bounds_diag(local_bbox), global_diag * 0.035),
        )
        fallback_point = to_screen(target)
        target_rect = (
            projected_bbox_rect_with(to_screen, evidence_bbox)
            if evidence_bbox is not None
            else None
        )
        annotation = build_screen_annotation(
            issue, fallback_point, width, height, target_rect
        )
        screen_annotations[issue.id] = annotation
        store_render_check(issue, "overview", annotation)
    return screen_annotations


def load_font(size: int) -> Any:
    from PIL import ImageFont

    agents_dir = Path(__file__).resolve().parents[3]
    bundled_font_dir = agents_dir / "water_mark" / "fonts"
    candidates = [
        bundled_font_dir / "NotoSansCJKsc-Regular.otf",
        bundled_font_dir / "NotoSansCJKsc-Bold.otf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def draw_arrow(
    draw: Any,
    start: tuple[int, int],
    end: tuple[int, int],
    fill: tuple[int, int, int],
    width: int,
) -> None:
    draw.line([start, end], fill=fill, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    size = 14
    left = (
        end[0] - size * math.cos(angle - math.pi / 6),
        end[1] - size * math.sin(angle - math.pi / 6),
    )
    right = (
        end[0] - size * math.cos(angle + math.pi / 6),
        end[1] - size * math.sin(angle + math.pi / 6),
    )
    draw.polygon([end, left, right], fill=fill)


def draw_text_box(
    draw: Any, xy: tuple[int, int], text: str, font: Any, color: tuple[int, int, int]
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0] + 18
    height = bbox[3] - bbox[1] + 12
    x, y = xy
    draw.rectangle(
        [x, y, x + width, y + height], fill=(255, 255, 255), outline=color, width=2
    )
    draw.text((x + 9, y + 6), text, fill=color, font=font)


def draw_measure_line(
    draw: Any,
    start: tuple[int, int],
    end: tuple[int, int],
    text: str,
    font: Any,
    color: tuple[int, int, int],
    *,
    arrowheads: bool = True,
) -> None:
    draw.line([start, end], fill=color, width=4)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    tick = 13
    normal = (-math.sin(angle), math.cos(angle))
    for point in (start, end):
        draw.line(
            [
                (int(point[0] - normal[0] * tick), int(point[1] - normal[1] * tick)),
                (int(point[0] + normal[0] * tick), int(point[1] + normal[1] * tick)),
            ],
            fill=color,
            width=3,
        )
    if arrowheads:
        draw_arrow(
            draw,
            (
                start[0] + int(math.cos(angle) * 24),
                start[1] + int(math.sin(angle) * 24),
            ),
            start,
            color,
            3,
        )
        draw_arrow(
            draw,
            (end[0] - int(math.cos(angle) * 24), end[1] - int(math.sin(angle) * 24)),
            end,
            color,
            3,
        )
    mid = ((start[0] + end[0]) // 2 + 12, (start[1] + end[1]) // 2 - 34)
    draw_text_box(draw, mid, text, font, color)


def annotate_png(
    base_path: Path,
    output_path: Path,
    annotations: list[tuple[Any, ...]],
    title: str | None = None,
    overlays: list[dict[str, Any]] | None = None,
    show_annotation_labels: bool = True,
    show_annotation_ids: bool = False,
) -> None:
    from PIL import Image, ImageDraw

    image = Image.open(base_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = load_font(20)
    title_font = load_font(22)
    red = (255, 42, 42)
    width, height = image.size

    if title:
        draw.rectangle(
            [14, 12, min(width - 14, 14 + len(title) * 18 + 28), 50],
            fill=(255, 255, 255),
            outline=red,
            width=2,
        )
        draw.text((28, 18), title, fill=red, font=title_font)

    for idx, annotation in enumerate(annotations):
        issue = annotation[0]
        point = annotation[1]
        radius = int(annotation[2]) if len(annotation) >= 3 else 34
        x = clamp(point[0], 20, width - 20)
        y = clamp(point[1], 20, height - 20)
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius], outline=red, width=4
        )

        if show_annotation_ids:
            short_id = issue.id.replace("DFM-", "")
            id_font = load_font(max(16, min(24, radius // 2)))
            text_bbox = draw.textbbox((0, 0), short_id, font=id_font)
            text_w = text_bbox[2] - text_bbox[0]
            text_h = text_bbox[3] - text_bbox[1]
            pad_x = 6
            pad_y = 4
            box = [
                x - text_w // 2 - pad_x,
                y - text_h // 2 - pad_y,
                x + text_w // 2 + pad_x,
                y + text_h // 2 + pad_y,
            ]
            draw.rectangle(box, fill=(255, 255, 255), outline=red, width=2)
            draw.text(
                (x - text_w // 2, y - text_h // 2 - 1), short_id, fill=red, font=id_font
            )

        if not show_annotation_labels:
            continue

        label = f"{issue.id} {issue.title}"
        bbox = draw.textbbox((0, 0), label, font=font)
        label_w = bbox[2] - bbox[0] + 22
        label_h = bbox[3] - bbox[1] + 18
        prefer_right = x < width * 0.58
        label_x = x + 72 if prefer_right else x - 72 - label_w
        label_y = y + 34 + idx * 8
        label_x = clamp(label_x, 8, width - label_w - 8)
        label_y = clamp(label_y, 58, height - label_h - 8)

        target = (label_x + (0 if prefer_right else label_w), label_y + label_h // 2)
        draw_arrow(draw, target, (x, y + radius - 4), red, 4)
        draw.rectangle(
            [label_x, label_y, label_x + label_w, label_y + label_h],
            fill=(255, 255, 255),
            outline=red,
            width=2,
        )
        draw.text((label_x + 11, label_y + 8), label, fill=red, font=font)

    for overlay in overlays or []:
        kind = overlay.get("kind")
        if kind == "measure":
            draw_measure_line(
                draw, overlay["start"], overlay["end"], overlay["text"], font, red
            )
        elif kind == "arrow":
            draw_arrow(draw, overlay["start"], overlay["end"], red, 4)
            draw_text_box(draw, overlay["label_at"], overlay["text"], font, red)
        elif kind == "line":
            draw.line([overlay["start"], overlay["end"]], fill=red, width=4)
            draw_text_box(draw, overlay["label_at"], overlay["text"], font, red)

    image.save(output_path)
    emit_artifact_event(output_path)


def make_preview_edge(occ: SimpleNamespace, start: Vec3, end: Vec3) -> Any | None:
    if distance(start, end) <= 1e-7:
        return None
    try:
        maker = occ.BRepBuilderAPI_MakeEdge(occ.gp_Pnt(*start), occ.gp_Pnt(*end))
        return maker.Edge()
    except Exception:
        return None


def bbox_preview_edges(occ: SimpleNamespace, bbox: BBox) -> list[Any]:
    corners = bbox_corners(bbox)
    pairs = [
        (0, 1),
        (0, 2),
        (0, 4),
        (3, 1),
        (3, 2),
        (3, 7),
        (5, 1),
        (5, 4),
        (5, 7),
        (6, 2),
        (6, 4),
        (6, 7),
    ]
    edges = []
    for first, second in pairs:
        edge = make_preview_edge(occ, corners[first], corners[second])
        if edge is not None:
            edges.append(edge)
    return edges


def expand_degenerate_bbox(bbox: BBox, minimum_span: float) -> BBox:
    center = bbox_center(bbox)
    spans = bbox_dimensions(bbox)
    half = [max(span / 2.0, minimum_span / 2.0) for span in spans]
    return (
        center[0] - half[0],
        center[1] - half[1],
        center[2] - half[2],
        center[0] + half[0],
        center[1] + half[1],
        center[2] + half[2],
    )


def issue_preview_edges(
    occ: SimpleNamespace,
    issue: Issue,
    evidence_bbox: BBox | None,
    global_diag: float,
    target: Vec3,
    local_diag: float,
) -> list[Any]:
    edges: list[Any] = []
    line_pairs = [
        ("measurement_start", "measurement_end"),
        ("hole_point", "outer_point"),
    ]
    for start_key, end_key in line_pairs:
        start = metric_vec3(issue.metric.get(start_key))
        end = metric_vec3(issue.metric.get(end_key))
        if start is not None and end is not None:
            edge = make_preview_edge(occ, start, end)
            if edge is not None:
                edges.append(edge)

    if edges:
        return edges

    points = issue_metric_points(issue, target, local_diag)
    if len(points) >= 2:
        edge = make_preview_edge(occ, points[0], points[-1])
        if edge is not None:
            edges.append(edge)
            return edges

    if evidence_bbox is not None:
        minimum_span = max(global_diag * 0.012, 0.8)
        marker_bbox = expand_degenerate_bbox(evidence_bbox, minimum_span)
        edges.extend(bbox_preview_edges(occ, marker_bbox))
    return edges


def thresholds_dict(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "process": args.process,
        "min_wall_mm": args.min_wall_mm,
        "min_hole_diameter_mm": args.min_hole_diameter_mm,
        "min_tool_radius_mm": args.min_tool_radius_mm,
        "min_slot_width_mm": args.min_slot_width_mm,
        "min_draft_deg": args.min_draft_deg,
        "max_planar_step_mm": args.max_planar_step_mm,
        "min_edge_mm": args.min_edge_mm,
        "min_face_area_mm2": args.min_face_area_mm2,
        "max_sliver_face_width_mm": args.max_sliver_face_width_mm,
        "min_sliver_face_length_mm": args.min_sliver_face_length_mm,
        "sliver_face_aspect_ratio": args.sliver_face_aspect_ratio,
        "local_boss_thickness_ratio": args.local_boss_thickness_ratio,
        "max_local_boss_wall_mm": args.max_local_boss_wall_mm,
        "max_local_boss_span_mm": args.max_local_boss_span_mm,
        "min_local_boss_side_area_mm2": args.min_local_boss_side_area_mm2,
        "min_local_boss_overlap_mm": args.min_local_boss_overlap_mm,
        "min_machining_gap_mm": args.min_machining_gap_mm,
        "min_machining_gap_overlap_mm": args.min_machining_gap_overlap_mm,
        "planar_step_parallel_tolerance_deg": args.planar_step_parallel_tolerance_deg,
        "min_planar_step_overlap_mm": args.min_planar_step_overlap_mm,
        "min_hole_edge_distance_mm": args.min_hole_edge_distance_mm,
        "min_hole_web_mm": args.min_hole_web_mm,
        "max_hole_depth_ratio": args.max_hole_depth_ratio,
        "deep_hole_max_diameter_mm": args.deep_hole_max_diameter_mm,
        "enable_thickness_field": args.enable_thickness_field,
        "max_wall_mm": args.max_wall_mm,
        "thickness_samples": args.thickness_samples,
        "thickness_issue_cluster_mm": args.thickness_issue_cluster_mm,
        "report_thickness_variation_with_thin_wall": args.report_thickness_variation_with_thin_wall,
        "thickness_variation_ratio": args.thickness_variation_ratio,
        "thickness_variation_nominal_max_mm": args.thickness_variation_nominal_max_mm,
        "enable_surface_continuity": args.enable_surface_continuity,
        "continuity_include_plane_plane": args.continuity_include_plane_plane,
        "continuity_include_plane_cylinder": args.continuity_include_plane_cylinder,
        "continuity_include_plane_other": args.continuity_include_plane_other,
        "continuity_include_cylinder_other": args.continuity_include_cylinder_other,
        "continuity_include_hard_edges": args.continuity_include_hard_edges,
        "continuity_g1_angle_deg": args.continuity_g1_angle_deg,
        "continuity_max_smooth_break_angle_deg": args.continuity_max_smooth_break_angle_deg,
        "continuity_g2_curvature_jump": args.continuity_g2_curvature_jump,
        "enable_undercut_slider": args.enable_undercut_slider,
        "undercut_negative_draft_deg": args.undercut_negative_draft_deg,
        "max_side_action_diameter_mm": args.max_side_action_diameter_mm,
        "side_action_surface_pull_abs_dot_max": args.side_action_surface_pull_abs_dot_max,
        "pull_dir": args.pull_dir,
    }


THRESHOLD_LABELS = {
    "process": "工艺类型",
    "min_wall_mm": "最小壁厚(mm)",
    "min_hole_diameter_mm": "最小孔径(mm)",
    "min_tool_radius_mm": "最小刀具半径(mm)",
    "min_slot_width_mm": "最小槽宽(mm)",
    "min_draft_deg": "最小拔模角(度)",
    "max_planar_step_mm": "最大平面台阶(mm)",
    "min_edge_mm": "最小边长(mm)",
    "min_face_area_mm2": "最小面面积(mm²)",
    "max_sliver_face_width_mm": "最大狭长面宽度(mm)",
    "min_sliver_face_length_mm": "最小狭长面长度(mm)",
    "sliver_face_aspect_ratio": "狭长面长宽比阈值",
    "local_boss_thickness_ratio": "局部凸台厚度倍率",
    "max_local_boss_wall_mm": "最大局部凸台壁厚(mm)",
    "max_local_boss_span_mm": "最大局部凸台跨度(mm)",
    "min_local_boss_side_area_mm2": "最小局部凸台侧面积(mm²)",
    "min_local_boss_overlap_mm": "最小局部凸台重叠(mm)",
    "min_machining_gap_mm": "最小加工间隙(mm)",
    "min_machining_gap_overlap_mm": "最小加工间隙重叠(mm)",
    "planar_step_parallel_tolerance_deg": "平面台阶平行容差(度)",
    "min_planar_step_overlap_mm": "最小平面台阶重叠(mm)",
    "min_hole_edge_distance_mm": "最小孔边距(mm)",
    "min_hole_web_mm": "最小孔间薄壁(mm)",
    "max_hole_depth_ratio": "最大孔深径比",
    "deep_hole_max_diameter_mm": "深孔最大直径(mm)",
    "enable_thickness_field": "启用厚度场检测",
    "max_wall_mm": "最大壁厚(mm)",
    "thickness_samples": "厚度采样数",
    "thickness_issue_cluster_mm": "厚度问题聚类距离(mm)",
    "report_thickness_variation_with_thin_wall": "薄壁同时报告厚度变化",
    "thickness_variation_ratio": "厚度变化倍率",
    "thickness_variation_nominal_max_mm": "厚度变化名义最大值(mm)",
    "enable_surface_continuity": "启用曲面连续性检测",
    "continuity_include_plane_plane": "连续性包含平面-平面",
    "continuity_include_plane_cylinder": "连续性包含平面-圆柱",
    "continuity_include_plane_other": "连续性包含平面-其他",
    "continuity_include_cylinder_other": "连续性包含圆柱-其他",
    "continuity_include_hard_edges": "连续性包含硬边",
    "continuity_g1_angle_deg": "G1 连续角度阈值(度)",
    "continuity_max_smooth_break_angle_deg": "最大平滑断裂角(度)",
    "continuity_g2_curvature_jump": "G2 曲率跳变阈值",
    "enable_undercut_slider": "启用倒扣/滑块检测",
    "undercut_negative_draft_deg": "倒扣负拔模角阈值(度)",
    "max_side_action_diameter_mm": "最大侧抽圆柱直径(mm)",
    "side_action_surface_pull_abs_dot_max": "侧抽面拔模方向点积阈值",
    "pull_dir": "拔模方向",
}

PROCESS_LABELS = {
    "generic": "通用",
    "injection": "注塑",
    "machining": "机加工",
}

DFM_STAGE_TITLES = [
    "基础几何与小特征扫描",
    "平面台阶与加工间隙",
    "面质量与碎面",
    "孔、圆柱与孔边距",
    "壁厚场与厚薄突变",
    "曲面连续性",
    "倒扣、拔模与侧抽",
]


DFM_STAGE_KEYS = [
    "geometry_and_small_features",
    "planar_steps_and_gaps",
    "surface_quality",
    "holes_and_edge_distance",
    "wall_thickness",
    "surface_continuity",
    "draft_and_undercut",
]


def format_threshold_value(key: str, value: Any) -> str:
    if key == "process":
        return PROCESS_LABELS.get(str(value), str(value))
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "未设置"
    if key == "pull_dir" and isinstance(value, (list, tuple)):
        return "(" + ", ".join(str(item) for item in value) + ")"
    return str(value)


def issue_value(issue: Issue | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(issue, dict):
        return issue.get(key, default)
    return getattr(issue, key, default)


def issue_table_header_lines() -> list[str]:
    return [
        "<table>",
        "<thead>",
        "<tr><th>ID</th><th>严重度</th><th>类型</th><th>位置(mm)</th><th>说明</th></tr>",
        "</thead>",
        "<tbody>",
    ]


def issue_table_row(issue: Issue | dict[str, Any]) -> str:
    issue_id = html.escape(str(issue_value(issue, "id", "")))
    severity = html.escape(str(issue_value(issue, "severity", "")))
    title = html.escape(str(issue_value(issue, "title", "")))
    anchor = html.escape(format_vec(issue_value(issue, "anchor")))
    message = html.escape(str(issue_value(issue, "message", "")))
    return (
        "<tr>"
        f"<td>{issue_id}</td>"
        f"<td>{severity}</td>"
        f"<td>{title}</td>"
        f"<td>{anchor}</td>"
        f"<td>{message}</td>"
        "</tr>"
    )


def issue_image_rows(issue: Issue | dict[str, Any], *, limit: int = 3) -> list[str]:
    images = issue_value(issue, "images", None)
    if not images:
        image = issue_value(issue, "image", None)
        images = [image] if image else []
    rows: list[str] = []
    for name in list(images)[:limit]:
        image_name = html.escape(str(name), quote=True)
        rows.append(
            '<tr><td colspan="5" style="text-align:center;">'
            f'<img src="{image_name}" alt="{image_name}" style="max-width:100%;height:auto;" />'
            "</td></tr>"
        )
    return rows


def issue_detail_lines(issue: Issue | dict[str, Any]) -> list[str]:
    return [
        *issue_table_header_lines(),
        issue_table_row(issue),
        *issue_image_rows(issue),
        "</tbody>",
        "</table>",
        "",
    ]


def emit_issue_detail(issue: Issue) -> None:
    for line in issue_detail_lines(issue):
        emit_report_delta(f"{line}\n")


def emit_stage_issue_details(
    title: str,
    issues: list[Issue],
    *,
    shape: Any | None = None,
    occ: SimpleNamespace | None = None,
    out_dir: Path | None = None,
    args: argparse.Namespace | None = None,
    global_bbox: BBox | None = None,
) -> None:
    emit_report_delta(f"#### {title} 图文说明\n\n")
    ordered_issues = sorted(issues, key=lambda issue: issue.id)
    # Defer targeted evidence until every geometry check has completed. The
    # worker can then apply a bounded evidence budget without dropping any
    # engineering findings.
    for issue in ordered_issues:
        emit_issue_detail(issue)
    return


def emit_stage_progress(title: str, *, completed: bool) -> None:
    try:
        index = DFM_STAGE_TITLES.index(title)
    except ValueError:
        index = 0
    key = DFM_STAGE_KEYS[index] if index < len(DFM_STAGE_KEYS) else "geometry_analysis"
    percent = 18 + index * 6 + (5 if completed else 0)
    emit_dfm_event("progress", stage=key, percent=min(percent, 62))


def mark_stage_issues(title: str, issues: list[Issue]) -> None:
    try:
        stage_order = DFM_STAGE_TITLES.index(title)
    except ValueError:
        stage_order = len(DFM_STAGE_TITLES)
    for issue in issues:
        issue.metric.setdefault("stage_title", title)
        issue.metric.setdefault("stage_order", stage_order)


def emit_analysis_intro() -> None:
    emit_report_delta("# DFM 分析报告\n\n")
    emit_report_delta("## 分阶段检测结果\n\n")


def emit_model_summary(stats: dict[str, Any]) -> None:
    topology = stats.get("topology", {})
    emit_report_delta("## 模型概况\n\n")
    emit_report_delta(
        f"- B-Rep 有效性: {'通过' if stats.get('valid_brep') else '存在问题'}\n"
    )
    emit_report_delta(f"- 外包围尺寸(mm): {format_vec(stats.get('bbox_size_mm'))}\n")
    emit_report_delta(
        "- 面/边/点数量: "
        f"{topology.get('faces', 0)} / {topology.get('edges', 0)} / {topology.get('vertices', 0)}\n\n"
    )


def emit_stage_start(title: str) -> None:
    emit_stage_progress(title, completed=False)
    emit_report_delta(f"### {title}\n\n")
    emit_report_delta("正在检测...\n\n")


def emit_stage_result(
    title: str,
    issues: list[Issue],
    *,
    shape: Any | None = None,
    occ: SimpleNamespace | None = None,
    out_dir: Path | None = None,
    args: argparse.Namespace | None = None,
    global_bbox: BBox | None = None,
) -> None:
    emit_stage_progress(title, completed=True)
    if not issues:
        emit_report_delta(f"{title}：未发现超过当前阈值的风险项。\n\n")
        return
    mark_stage_issues(title, issues)
    emit_report_delta(f"{title}：发现 {len(issues)} 个风险项。\n\n")
    emit_stage_issue_details(
        title,
        issues,
        shape=shape,
        occ=occ,
        out_dir=out_dir,
        args=args,
        global_bbox=global_bbox,
    )


def format_vec(value: Any) -> str:
    if value is None:
        return ""
    return "(" + ", ".join(f"{float(item):.3f}" for item in value) + ")"


def default_out_dir(input_path: Path) -> Path:
    return input_path.with_suffix("").parent / f"{input_path.with_suffix('').name}_dfm"


def clean_previous_outputs(out_dir: Path) -> None:
    generated_patterns = [
        "issue_dfm-*.png",
        ".dfm-*_raw.png",
        "DFM-*.png",
        ".DFM-*_raw.png",
        "overview.png",
        "model.png",
        "dfm_highlighted.step",
        "dfm_highlighted.stp",
        "[0-9]*.step",
        "[0-9]*.stp",
        "dfm_report.json",
        "dfm_report.md",
    ]
    for pattern in generated_patterns:
        for path in out_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def launch_preview_from_analyzer(
    preview_input: Path, args: argparse.Namespace
) -> dict[str, Any]:
    preview_script = Path(__file__).with_name("dfm_preview.py")
    command = [
        sys.executable,
        str(preview_script),
        str(preview_input),
        "--backend",
        args.preview_backend,
    ]
    if args.preview_plain:
        command.append("--plain")
    if args.preview_wait:
        command.append("--wait")

    process = subprocess.Popen(command)
    if args.preview_wait:
        return_code = process.wait()
    else:
        return_code = None
    return {
        "script": str(preview_script),
        "input": str(preview_input),
        "backend": args.preview_backend,
        "pid": process.pid,
        "return_code": return_code,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze STEP/STP CAD files for first-pass DFM issues."
    )
    parser.add_argument("input", type=Path, help="Input STEP/STP file.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for report and PNG files.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="JSON file with process/threshold defaults. CLI options override config values.",
    )
    parser.add_argument(
        "--operation", action="append", default=[], help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--highlight-step-name",
        default=None,
        help="Colored STEP preview filename written in the output directory.",
    )
    parser.add_argument("--highlight-step", dest="highlight_step", action="store_true")
    parser.add_argument(
        "--no-highlight-step", dest="highlight_step", action="store_false"
    )
    parser.add_argument(
        "--open-preview",
        action="store_true",
        help="Open the highlighted STEP in an external CAD previewer after analysis.",
    )
    parser.add_argument(
        "--preview-backend",
        choices=["auto", "freecad", "pythonocc", "cadquery"],
        default="auto",
    )
    parser.add_argument(
        "--preview-wait",
        action="store_true",
        help="Wait for the preview process to exit.",
    )
    parser.add_argument(
        "--preview-plain",
        action="store_true",
        help="Preview the original STEP instead of dfm_highlighted.step.",
    )
    parser.add_argument(
        "--process", choices=["generic", "injection", "machining"], default="generic"
    )
    parser.add_argument("--min-wall-mm", type=float, default=1.0)
    parser.add_argument("--min-hole-diameter-mm", type=float, default=2.0)
    parser.add_argument("--min-tool-radius-mm", type=float, default=0.5)
    parser.add_argument("--min-slot-width-mm", type=float, default=1.0)
    parser.add_argument("--min-draft-deg", type=float, default=None)
    parser.add_argument("--max-planar-step-mm", type=float, default=0.5)
    parser.add_argument("--min-edge-mm", type=float, default=0.05)
    parser.add_argument("--min-face-area-mm2", type=float, default=0.05)
    parser.add_argument("--max-sliver-face-width-mm", type=float, default=0.5)
    parser.add_argument("--min-sliver-face-length-mm", type=float, default=2.0)
    parser.add_argument("--sliver-face-aspect-ratio", type=float, default=20.0)
    parser.add_argument("--local-boss-thickness-ratio", type=float, default=4.0)
    parser.add_argument("--max-local-boss-wall-mm", type=float, default=4.0)
    parser.add_argument("--max-local-boss-span-mm", type=float, default=25.0)
    parser.add_argument("--min-local-boss-side-area-mm2", type=float, default=250.0)
    parser.add_argument("--min-local-boss-overlap-mm", type=float, default=8.0)
    parser.add_argument("--min-machining-gap-mm", type=float, default=0.8)
    parser.add_argument("--min-machining-gap-overlap-mm", type=float, default=1.0)
    parser.add_argument("--planar-step-parallel-tolerance-deg", type=float, default=1.0)
    parser.add_argument("--min-planar-step-overlap-mm", type=float, default=5.0)
    parser.add_argument("--min-hole-edge-distance-mm", type=float, default=1.0)
    parser.add_argument("--min-hole-web-mm", type=float, default=1.0)
    parser.add_argument("--max-hole-depth-ratio", type=float, default=6.0)
    parser.add_argument("--deep-hole-max-diameter-mm", type=float, default=8.0)
    parser.add_argument(
        "--engineering",
        action="store_true",
        help="Compatibility flag; engineering-grade modules run by default.",
    )
    parser.add_argument(
        "--enable-thickness-field", dest="enable_thickness_field", action="store_true"
    )
    parser.add_argument(
        "--disable-thickness-field", dest="enable_thickness_field", action="store_false"
    )
    parser.add_argument("--thickness-samples", type=int, default=450)
    parser.add_argument("--thickness-mesh-deflection-mm", type=float, default=0.8)
    parser.add_argument("--thickness-min-hit-mm", type=float, default=0.10)
    parser.add_argument("--max-thickness-issues", type=int)
    parser.add_argument("--thickness-issue-cluster-mm", type=float, default=0.0)
    parser.add_argument(
        "--report-thickness-variation-with-thin-wall", action="store_true"
    )
    parser.add_argument("--max-wall-mm", type=float, default=0.0)
    parser.add_argument("--thickness-variation-ratio", type=float, default=4.0)
    parser.add_argument("--thickness-variation-nominal-max-mm", type=float, default=6.0)
    parser.add_argument(
        "--enable-surface-continuity",
        dest="enable_surface_continuity",
        action="store_true",
    )
    parser.add_argument(
        "--disable-surface-continuity",
        dest="enable_surface_continuity",
        action="store_false",
    )
    parser.add_argument(
        "--continuity-include-plane-plane",
        action="store_true",
        help="Also report sharp plane-plane edges as surface continuity findings.",
    )
    parser.add_argument(
        "--continuity-include-plane-cylinder",
        action="store_true",
        help="Also report plane-cylinder hard edges, such as hole mouths, as surface continuity findings.",
    )
    parser.add_argument(
        "--continuity-include-plane-other",
        action="store_true",
        help="Also report hard edges between planes and other surface types as surface continuity findings.",
    )
    parser.add_argument(
        "--continuity-include-cylinder-other",
        action="store_true",
        help="Also report hard edges between cylinders and other surface types as surface continuity findings.",
    )
    parser.add_argument(
        "--continuity-include-hard-edges",
        action="store_true",
        help="Also report large-angle intentional hard edges as continuity findings.",
    )
    parser.add_argument("--continuity-g1-angle-deg", type=float, default=35.0)
    parser.add_argument(
        "--continuity-max-smooth-break-angle-deg", type=float, default=35.0
    )
    parser.add_argument("--continuity-g1-high-angle-deg", type=float, default=75.0)
    parser.add_argument("--continuity-g2-curvature-jump", type=float, default=0.08)
    parser.add_argument(
        "--enable-undercut-slider", dest="enable_undercut_slider", action="store_true"
    )
    parser.add_argument(
        "--disable-undercut-slider", dest="enable_undercut_slider", action="store_false"
    )
    parser.add_argument("--undercut-negative-draft-deg", type=float, default=0.5)
    parser.add_argument("--undercut-side-face-abs-dot-max", type=float, default=0.70)
    parser.add_argument("--undercut-min-area-mm2", type=float, default=1.0)
    parser.add_argument("--side-core-axis-pull-abs-dot-max", type=float, default=0.25)
    parser.add_argument(
        "--side-action-surface-pull-abs-dot-max", type=float, default=0.35
    )
    parser.add_argument(
        "--max-hole-draft-undercut-diameter-mm",
        type=float,
        default=12.0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--min-hole-draft-cap-area-mm2", type=float, default=0.5, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--max-hole-draft-cap-area-mm2",
        type=float,
        default=12.0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-hole-draft-cap-depth-mm", type=float, default=1.0, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--max-hole-draft-cap-aspect", type=float, default=1.8, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--min-hole-draft-side-area-mm2",
        type=float,
        default=3.0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--max-side-action-diameter-mm", type=float, default=30.0)
    parser.add_argument("--min-draft-area-mm2", type=float, default=1.0)
    parser.add_argument("--pull-dir", type=parse_vec3, default=(0.0, 0.0, 1.0))
    parser.add_argument("--parallel-tolerance-deg", type=float, default=5.0)
    parser.add_argument("--angle-tolerance-deg", type=float, default=0.05)
    parser.add_argument("--model-tolerance-mm", type=float, default=0.01)
    parser.add_argument("--max-issues", type=int)
    parser.add_argument("--max-issues-per-code", type=int)
    parser.add_argument(
        "--max-evidence-issues",
        type=int,
        default=None,
        help="Render targeted evidence for at most this many severity-sorted findings.",
    )
    parser.add_argument("--image-width", type=int, default=1280)
    parser.add_argument("--image-height", type=int, default=860)
    parser.add_argument("--mesh-deflection-mm", type=float, default=0.5)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--fail-on-issues", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.set_defaults(
        enable_thickness_field=True,
        enable_surface_continuity=True,
        enable_undercut_slider=True,
        highlight_step=True,
    )
    return parser


def config_action_map(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    actions: dict[str, argparse.Action] = {}
    for action in parser._actions:
        if action.dest in {argparse.SUPPRESS, "help", "input", "config"}:
            continue
        actions[action.dest] = action
    return actions


def normalize_config_key(key: str) -> str:
    return key.strip().lstrip("-").replace("-", "_")


def coerce_config_value(action: argparse.Action, value: Any) -> Any:
    if action.dest == "pull_dir":
        return parse_vec3_config(value)
    if action.choices is not None and value not in action.choices:
        raise ValueError(f"{action.dest} must be one of {sorted(action.choices)}")
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        if not isinstance(value, bool):
            raise ValueError(f"{action.dest} must be true or false")
        return value
    if action.type is Path:
        return Path(value)
    if action.type is not None and value is not None:
        try:
            return action.type(value)
        except Exception as exc:
            raise ValueError(f"{action.dest}={value!r} is invalid: {exc}") from exc
    return value


def load_config_defaults(
    config_path: Path, parser: argparse.ArgumentParser
) -> dict[str, Any]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot read JSON config {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")

    raw_values: dict[str, Any] = {}
    thresholds = data.get("thresholds")
    if thresholds is not None:
        if not isinstance(thresholds, dict):
            raise ValueError("config field 'thresholds' must be an object")
        raw_values.update(thresholds)
    for key, value in data.items():
        if key in {"thresholds", "name", "description", "notes", "version"}:
            continue
        raw_values[key] = value

    actions = config_action_map(parser)
    defaults: dict[str, Any] = {}
    unknown: list[str] = []
    for raw_key, value in raw_values.items():
        key = normalize_config_key(str(raw_key))
        action = actions.get(key)
        if action is None:
            unknown.append(str(raw_key))
            continue
        defaults[key] = coerce_config_value(action, value)
    if unknown:
        raise ValueError(f"unknown config option(s): {', '.join(sorted(unknown))}")
    return defaults


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    if args.engineering:
        args.enable_thickness_field = True
        args.enable_surface_continuity = True
        args.enable_undercut_slider = True
    if args.min_draft_deg is None:
        args.min_draft_deg = 1.0 if args.process == "injection" else 0.0
    if args.process == "machining" and args.min_tool_radius_mm < 0.8:
        args.min_tool_radius_mm = 0.8
    args.out = args.out or default_out_dir(args.input)
    highlight_name = Path(
        str(args.highlight_step_name or f"{int(time.time() * 1000)}.step")
    ).name
    if not highlight_name.lower().endswith((".step", ".stp")):
        highlight_name += ".step"
    args.highlight_step_name = highlight_name
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    preliminary_args, _unknown = parser.parse_known_args(argv)
    if preliminary_args.config is not None:
        try:
            parser.set_defaults(**load_config_defaults(preliminary_args.config, parser))
        except ValueError as exc:
            parser.error(str(exc))
    args = normalize_args(parser.parse_args(argv))
    input_path = args.input.resolve()
    out_dir = args.out.resolve()

    if not input_path.exists():
        parser.error(f"Input file does not exist: {input_path}")
    if input_path.suffix.lower() not in {".stp", ".step"}:
        parser.error("Only STEP/STP input is supported by this analyzer.")

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        clean_previous_outputs(out_dir)
        emit_analysis_intro()
        emit_dfm_event("progress", stage="load_dependencies", percent=5)
        occ = import_occ()
        emit_dfm_event("progress", stage="load_geometry", percent=10)
        shape = read_step(input_path, occ)
        emit_dfm_event("progress", stage="inspect_geometry", percent=15)
        issues, stats = analyze_shape(shape, occ, args, out_dir)
        should_render = operation_enabled(args, "render_evidence")
        evidence_result = EvidenceResult(0, None, None)
        if should_render:
            evidence_result = render_evidence_bundle(shape, occ, issues, out_dir, args)

        result = {
            "input": str(input_path),
            "output_dir": str(out_dir),
            "backends": probe_backends(),
            "thresholds": thresholds_dict(args),
            "stats": stats,
            "issue_count": len(issues),
            "issues": [asdict(issue) for issue in issues],
            "evidence": {
                "rendered_findings": evidence_result.rendered_findings,
                "total_findings": len(issues),
                "limit": args.max_evidence_issues,
            },
            "highlighted_step": evidence_result.highlighted_step,
            "highlighted_step_error": evidence_result.highlighted_step_error,
        }
        emit_dfm_event("progress", stage="write_reports", percent=95)
        json_report = out_dir / "dfm_report.json"
        markdown_report = out_dir / "dfm_report.md"
        write_json_report(json_report, result)
        emit_artifact_event(json_report, "report_json")
        write_markdown_report(markdown_report, result, emit_stream=False)
        emit_artifact_event(markdown_report, "report_markdown")
    except Exception as exc:
        if args.debug:
            traceback.print_exc()
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    print(f"DFM analysis complete: {len(issues)} issue(s)")
    if args.open_preview:
        try:
            preview_launch = launch_preview_from_analyzer(out_dir, args)
            print(
                f"Preview: backend={preview_launch['backend']} pid={preview_launch['pid']}"
            )
        except Exception as exc:
            print(f"[warn] preview launch failed: {exc}", file=sys.stderr)
    if issues and args.fail_on_issues:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
