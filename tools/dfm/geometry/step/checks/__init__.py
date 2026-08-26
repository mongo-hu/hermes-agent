"""STEP check-family catalog used by plans, diagnostics, and tests."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckFamily:
    key: str
    operation: str
    result_kind: str


CHECK_FAMILIES = (
    CheckFamily("topology", "inspect_topology", "measurement"),
    CheckFamily("small_features", "inspect_small_features", "measurement"),
    CheckFamily("planar_spacing", "measure_planar_spacing", "measurement"),
    CheckFamily("face_quality", "inspect_face_quality", "measurement"),
    CheckFamily("cylindrical", "inspect_cylindrical_features", "measurement"),
    CheckFamily("thickness", "measure_wall_thickness", "measurement"),
    CheckFamily("draft", "measure_draft", "measurement"),
    CheckFamily("continuity", "inspect_surface_continuity", "measurement"),
    CheckFamily("undercut", "inspect_undercut", "candidate"),
)

CHECK_FAMILY_BY_OPERATION = {item.operation: item for item in CHECK_FAMILIES}
