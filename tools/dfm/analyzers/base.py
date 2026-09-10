"""Shared analyzer protocol and cooperative cancellation primitive."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence, runtime_checkable

from ..contracts import ArtifactRecord, Capability, InputRecord, PlanRecord, WorkerEvent
from ..errors import DFMError


@dataclass(frozen=True)
class AnalyzerContext:
    project_id: str
    project_dir: Path
    input_mode: str | None
    inputs: Sequence[InputRecord]
    run_id: str = ""
    plan: PlanRecord | None = None
    event_sink: Callable[[WorkerEvent], None] | None = None


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise DFMError("run_cancelled", "The DFM run was cancelled.")


@runtime_checkable
class Analyzer(Protocol):
    key: str
    version: str
    supported_inputs: tuple[str, ...]

    def capability(self, context: AnalyzerContext) -> Capability: ...

    def run(
        self,
        context: AnalyzerContext,
        cancellation: CancellationToken,
    ) -> list[ArtifactRecord]: ...
