"""Stable geometry-reader registry used during DFM input registration."""

from .registry import GeometryReaderRegistry, build_default_geometry_reader_registry

__all__ = ["GeometryReaderRegistry", "build_default_geometry_reader_registry"]

