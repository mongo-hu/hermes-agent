"""Concrete renderer registry used by the DFM worker report pipeline."""

from __future__ import annotations

from .base import ReportRenderer
from ..errors import DFMError


class ReportRendererRegistry:
    def __init__(self) -> None:
        self._renderers: dict[str, ReportRenderer] = {}

    def register(self, renderer: ReportRenderer) -> None:
        key = renderer.key.strip().lower()
        if not key or key in self._renderers:
            raise DFMError(
                "report_renderer_invalid",
                f"DFM report renderer is empty or already registered: {key}",
            )
        self._renderers[key] = renderer

    def get(self, key: str) -> ReportRenderer:
        try:
            return self._renderers[key.strip().lower()]
        except KeyError as exc:
            raise DFMError(
                "report_renderer_not_found",
                f"DFM report renderer is not registered: {key}",
            ) from exc


def build_default_report_registry() -> ReportRendererRegistry:
    from .pptx import PythonPptxReportRenderer

    registry = ReportRendererRegistry()
    registry.register(PythonPptxReportRenderer())
    return registry
