"""Geometry readers selected by stable input format identifiers."""

from __future__ import annotations

from ...errors import DFMError
from ..contracts import GeometryReader


class GeometryReaderRegistry:
    def __init__(self) -> None:
        self._readers: dict[str, GeometryReader] = {}

    def register(self, reader: GeometryReader) -> None:
        for format_id in reader.format_ids:
            if not format_id or format_id in self._readers:
                raise DFMError(
                    "geometry_reader_invalid",
                    "Geometry format is empty or already registered.",
                    {"format_id": format_id},
                )
            self._readers[format_id] = reader

    def get(self, format_id: str) -> GeometryReader:
        try:
            return self._readers[format_id]
        except KeyError as exc:
            raise DFMError(
                "input_type_unsupported",
                f"Geometry format is not supported: {format_id}",
                {"format_id": format_id, "supported_formats": list(self.keys())},
            ) from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._readers))


def build_default_geometry_reader_registry() -> GeometryReaderRegistry:
    from .step import StepGeometryReader

    registry = GeometryReaderRegistry()
    registry.register(StepGeometryReader())
    return registry

