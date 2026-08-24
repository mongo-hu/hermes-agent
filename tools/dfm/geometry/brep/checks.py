"""Stable calculator identifiers shared by precise B-Rep input readers."""

from __future__ import annotations

from ...errors import DFMError


BREP_CHECK_FAMILIES = {
    "load_geometry": "load",
    "inspect_topology": "topology",
    "inspect_small_features": "small_features",
    "measure_planar_spacing": "planar_spacing",
    "inspect_face_quality": "face_quality",
    "inspect_cylindrical_features": "cylindrical",
    "measure_wall_thickness": "thickness",
    "measure_draft": "draft",
    "inspect_surface_continuity": "continuity",
    "inspect_undercut": "undercut",
    "render_evidence": "evidence",
}


def resolve_brep_check(calculator_id: str) -> str:
    try:
        return BREP_CHECK_FAMILIES[calculator_id]
    except KeyError as exc:
        raise DFMError(
            "unsupported_capability",
            "The DFM plan contains an unsupported STEP operation.",
            {"calculator_id": calculator_id, "representation": "brep"},
        ) from exc
