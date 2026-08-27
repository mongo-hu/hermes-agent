"""Deterministic registry for current reference, deferred, and injected analyzers."""

from __future__ import annotations

from .base import Analyzer
from .drawing import DrawingAnalyzer
from .fusion import FusionAnalyzer
from .parasolid import ParasolidAnalyzer
from .step import StepAnalyzer
from ..backends.nx.client import HttpNXBackendClient
from ..config import DFMConfig
from ..errors import DFMError


class AnalyzerRegistry:
    def __init__(self) -> None:
        self._analyzers: dict[str, Analyzer] = {}

    def register(self, analyzer: Analyzer) -> None:
        if analyzer.key in self._analyzers:
            raise DFMError(
                "analyzer_duplicate",
                f"DFM analyzer is already registered: {analyzer.key}",
                {"analyzer_key": analyzer.key},
            )
        self._analyzers[analyzer.key] = analyzer

    def get(self, key: str) -> Analyzer:
        try:
            return self._analyzers[key]
        except KeyError as exc:
            raise DFMError(
                "analyzer_not_found",
                f"DFM analyzer is not registered: {key}",
                {"analyzer_key": key},
            ) from exc

    def keys(self) -> list[str]:
        return sorted(self._analyzers)


def build_default_registry(config: DFMConfig | None = None) -> AnalyzerRegistry:
    config = config or DFMConfig()
    registry = AnalyzerRegistry()
    registry.register(
        StepAnalyzer(
            python_executable=None
            if config.runtime_python == "auto"
            else config.runtime_python,
            timeout_seconds=config.timeout_seconds,
        )
    )
    registry.register(
        DrawingAnalyzer(
            enabled=config.drawing_enabled,
            max_pages=config.max_pages,
            model_name=config.drawing_model,
            base_url=config.drawing_base_url,
            timeout_seconds=config.drawing_request_timeout_seconds,
        )
    )
    registry.register(FusionAnalyzer())
    nx_client = (
        HttpNXBackendClient(
            config.nx_endpoint,
            timeout_seconds=config.nx_request_timeout_seconds,
        )
        if config.nx_endpoint
        else None
    )
    registry.register(
        ParasolidAnalyzer(
            nx_client,
            poll_interval_seconds=config.nx_poll_interval_seconds,
        )
    )
    return registry
