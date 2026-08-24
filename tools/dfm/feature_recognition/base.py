"""Hermes-owned provider contract for semantic feature recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ..contracts import FeatureRecord, InputRecord, ObservationRecord, RegionRecord


@dataclass(frozen=True)
class FeatureRecognitionResult:
    features: list[FeatureRecord]
    regions: list[RegionRecord]
    diagnostics: dict[str, Any]
    observations: list[ObservationRecord] = field(default_factory=list)


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
    ) -> FeatureRecognitionResult: ...

