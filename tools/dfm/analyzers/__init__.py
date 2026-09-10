"""DFM analyzer extension boundary."""

from .base import Analyzer, AnalyzerContext, CancellationToken
from .registry import AnalyzerRegistry, build_default_registry

__all__ = [
    "Analyzer",
    "AnalyzerContext",
    "AnalyzerRegistry",
    "CancellationToken",
    "build_default_registry",
]
