"""Format-independent geometry reader and representation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..contracts import Capability


@dataclass(frozen=True)
class GeometryMetadata:
    format_id: str
    representation: str
    units: str | None = None
    model_tolerance: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GeometryReader(Protocol):
    key: str
    format_ids: tuple[str, ...]

    def capability(self) -> Capability: ...

    def preflight(self, path: Path) -> GeometryMetadata: ...
