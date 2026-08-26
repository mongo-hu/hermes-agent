"""STEP reader metadata boundary around the existing validated intake."""

from pathlib import Path

from ...contracts import Capability, CapabilityStatus
from ...project.step_preflight import inspect_step
from ..contracts import GeometryMetadata


class StepGeometryReader:
    key = "opencascade_step"
    format_ids = ("step",)

    def capability(self) -> Capability:
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "STEP intake and the OpenCascade worker boundary are implemented.",
            details={"format_ids": list(self.format_ids), "representation": "brep"},
        )

    def preflight(self, path: Path) -> GeometryMetadata:
        details = inspect_step(path)
        return GeometryMetadata("step", "brep", details=details)
