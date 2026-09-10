"""Application-level report generation after deterministic DFM analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .base import ReportArtifact, ReportContext
from .registry import ReportRendererRegistry, build_default_report_registry


def render_default_reports(
    *,
    artifact_dir: Path,
    result: Mapping[str, Any],
    process: str,
    scope_id: str,
    formats: tuple[str, ...] = ("pptx",),
    registry: ReportRendererRegistry | None = None,
) -> list[ReportArtifact]:
    """Render the configured built-in formats from one immutable result."""

    context = ReportContext(
        artifact_dir=artifact_dir.resolve(),
        result=result,
        process=process,
        scope_id=scope_id,
    )
    selected = registry or build_default_report_registry()
    return [selected.get(key).render(context) for key in formats]
