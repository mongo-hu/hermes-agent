"""Stable contracts shared by DFM report renderers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ReportContext:
    """Immutable engineering result and the directory containing its evidence."""

    artifact_dir: Path
    result: Mapping[str, Any]
    process: str
    scope_id: str
    max_images_per_finding: int = 3


@dataclass(frozen=True)
class ReportArtifact:
    """A renderer output ready to enter the worker artifact contract."""

    kind: str
    path: Path
    media_type: str


class ReportRenderer(ABC):
    """Format-specific report renderer used by the deterministic pipeline."""

    key: str

    @abstractmethod
    def render(self, context: ReportContext) -> ReportArtifact:
        """Render one report artifact from an already-computed DFM result."""
