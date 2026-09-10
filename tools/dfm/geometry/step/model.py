"""Shared, immutable per-run STEP topology index for M1.2 checks."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class FaceEntry:
    index: int
    shape: Any
    surface: Any
    surface_type: Any
    area: float
    center: tuple[float, float, float]
    bbox: tuple[float, float, float, float, float, float]
    info: Any


@dataclass(frozen=True)
class EdgeEntry:
    index: int
    shape: Any
    curve: Any
    curve_type: Any
    length: float
    center: tuple[float, float, float]


@dataclass(frozen=True)
class LoadedStepModel:
    shape: Any
    bbox: tuple[float, float, float, float, float, float]
    stats: dict[str, Any]
    faces: tuple[FaceEntry, ...]
    edges: tuple[EdgeEntry, ...]
    plane_faces: tuple[Any, ...]

    @classmethod
    def build(cls, shape: Any, occ: SimpleNamespace) -> "LoadedStepModel":
        # Imported lazily so the compatibility module can adopt this index without
        # changing how OpenCascade is loaded in the isolated worker.
        from . import legacy_analyzer as legacy

        bbox = legacy.shape_bbox(shape, occ)
        stats = {
            "bbox": bbox,
            "bbox_size_mm": legacy.bbox_size(bbox),
            "bbox_center": legacy.bbox_center(bbox),
            "topology": legacy.count_topology(shape, occ),
            "area_mm2": legacy.shape_area(shape, occ),
            "volume_mm3": legacy.shape_volume(shape, occ),
            "valid_brep": legacy.is_valid_shape(shape, occ),
        }
        faces: list[FaceEntry] = []
        plane_faces: list[Any] = []
        for index, raw_face in enumerate(
            legacy.iter_shapes(shape, occ.TopAbs_FACE, occ), start=1
        ):
            face = occ.topods.Face(raw_face)
            try:
                area, center = legacy.face_props(face, occ)
                surface = occ.BRepAdaptor_Surface(face)
                surface_type = surface.GetType()
                face_bbox = legacy.shape_bbox(face, occ)
                info = legacy.face_surface_info(
                    face, surface, surface_type, index, area, center, face_bbox, occ
                )
            except Exception:
                continue
            faces.append(
                FaceEntry(
                    index, face, surface, surface_type, area, center, face_bbox, info
                )
            )
            if surface_type == occ.GeomAbs_Plane:
                plane = surface.Plane()
                normal = legacy.dir_to_tuple(plane.Axis().Direction())
                if face.Orientation() == occ.TopAbs_REVERSED:
                    normal = legacy.mul(normal, -1.0)
                point = legacy.gp_to_tuple(plane.Location())
                plane_faces.append(
                    legacy.PlaneFace(
                        index,
                        normal,
                        point,
                        legacy.dot(normal, point),
                        area,
                        face_bbox,
                        center,
                    )
                )

        edges: list[EdgeEntry] = []
        for index, raw_edge in enumerate(
            legacy.iter_shapes(shape, occ.TopAbs_EDGE, occ), start=1
        ):
            edge = occ.topods.Edge(raw_edge)
            try:
                length, center = legacy.edge_props(edge, occ)
                curve = occ.BRepAdaptor_Curve(edge)
                curve_type = curve.GetType()
            except Exception:
                continue
            edges.append(EdgeEntry(index, edge, curve, curve_type, length, center))
        return cls(shape, bbox, stats, tuple(faces), tuple(edges), tuple(plane_faces))
