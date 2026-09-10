"""Deterministic registry for supported DFM manufacturing processes."""

from __future__ import annotations

from ..errors import DFMError
from ..ontology import LocalOntologyStore
from .base import ProcessAdapter


class ProcessAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProcessAdapter] = {}

    def register(self, adapter: ProcessAdapter) -> None:
        key = str(adapter.key).strip()
        if not key or key in self._adapters:
            raise DFMError(
                "process_adapter_invalid",
                "DFM process adapter key is empty or already registered.",
                {"process": key},
            )
        self._adapters[key] = adapter

    def get(self, key: str) -> ProcessAdapter:
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise DFMError(
                "unsupported_capability",
                f"DFM process is not supported: {key}",
                {
                    "requested_process": key,
                    "supported_processes": list(self.keys()),
                },
            ) from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


def build_default_process_registry(
    ontology_store: LocalOntologyStore | None = None,
) -> ProcessAdapterRegistry:
    from .die_casting import DieCastingProcessAdapter
    from .injection import InjectionProcessAdapter

    registry = ProcessAdapterRegistry()
    registry.register(InjectionProcessAdapter(ontology_store=ontology_store))
    registry.register(DieCastingProcessAdapter())
    return registry
