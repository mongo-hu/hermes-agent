"""Provider contract for concrete semantic feature recognizers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ..contracts import (
    ArtifactRecord,
    FeatureRecord,
    InputRecord,
    ObservationRecord,
    RegionRecord,
)


@dataclass(frozen=True)
class FeatureRecognitionResult:
    features: list[FeatureRecord]
    regions: list[RegionRecord]
    diagnostics: dict[str, Any]
    observations: list[ObservationRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    topology_snapshot_id: str = ""
    render_mesh_snapshot_id: str = ""
    geometry_snapshot_ref: str = ""


@runtime_checkable
class FeatureRecognitionProvider(Protocol):
    key: str
    version: str

    def capability(self) -> dict[str, Any]: ...

    def recognize(
        self,
        input_record: InputRecord,
        *,
        process: str,
        facts: Mapping[str, Any] | None = None,
        project_dir: Path | None = None,
    ) -> FeatureRecognitionResult: ...
