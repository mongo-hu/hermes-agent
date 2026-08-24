"""Dependency-free STEP intake metadata boundary before the OCCT process."""

from pathlib import Path

from ...contracts import Capability, CapabilityStatus
from ...project.step_preflight import inspect_step
from ..contracts import GeometryMetadata


class StepGeometryReader:
    key = "step_lexical_preflight"
    format_ids = ("step",)

    def capability(self) -> Capability:
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "STEP lexical intake is available; OCCT owns authoritative B-Rep loading.",
            details={
                "format_ids": list(self.format_ids),
                "representation": "brep",
                "pythonocc": "deprecated_disabled",
                "authoritative_geometry_backend": "occt_cpp_external_process",
            },
        )

    def preflight(self, path: Path) -> GeometryMetadata:
        details = inspect_step(path)
        return GeometryMetadata("step", "brep", details=details)

