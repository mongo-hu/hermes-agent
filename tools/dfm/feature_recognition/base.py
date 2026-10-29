"""Provider contract for concrete semantic feature recognizers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ..contracts import (
    ArtifactRecord,
    FeatureRecord,
    InputRecord,
    ObservationRecord,
)


@dataclass(frozen=True)
class FeatureRecognitionResult:
    features: list[FeatureRecord]
    diagnostics: dict[str, Any]
    observations: list[ObservationRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    topology_snapshot_id: str = ""
    render_mesh_snapshot_id: str = ""
    geometry_snapshot_ref: str = ""
    worker_snapshot_id: str = ""
    annotations: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    forming_scopes: list[dict[str, Any]] = field(default_factory=list)


def reconcile_worker_features(
    previous: list[FeatureRecord], current: list[FeatureRecord], input_sha256: str
) -> list[FeatureRecord]:
    """Keep human metadata, but never reconstruct geometry from an edit request."""
    by_id = {item.feature_id: item for item in previous if item.input_sha256 == input_sha256}

    def reviewed_content(item: FeatureRecord) -> tuple:
        return (
            item.kind,
            item.properties.get("subtype"),
            item.properties.get("geometry_refs", []),
            item.properties.get("parts", {}),
            item.properties.get("parameters", {}),
        )

    merged = [item for item in previous if item.input_sha256 != input_sha256]
    for item in current:
        old = by_id.get(item.feature_id)
        if old is not None:
            item = replace(
                item,
                properties={**old.properties, **item.properties},
                status=old.status if reviewed_content(old) == reviewed_content(item) else "candidate",
            )
        merged.append(item)
    return merged


@runtime_checkable
class FeatureRecognitionProvider(Protocol):
    key: str
    version: str

    def capability(self, project_id: str = "") -> dict[str, Any]: ...

    def recognize(
        self,
        input_record: InputRecord,
        *,
        process: str,
        facts: Mapping[str, Any] | None = None,
        project_dir: Path | None = None,
    ) -> FeatureRecognitionResult: ...
