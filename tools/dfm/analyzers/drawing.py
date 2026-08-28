"""Adapter from isolated 2D OCR to Agent-readable evidence artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from ..contracts import (
    WORKER_SCHEMA_VERSION,
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    InputRecord,
    WorkerEvent,
)
from ..drawing_pipeline.interface import (
    DRAWING_PIPELINE_VERSION,
    DrawingPipelineError,
    DrawingPipelineResult,
    execute_2d_pipeline,
    pipeline_capability,
)
from ..errors import DFMError
from .base import AnalyzerContext, CancellationToken


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(
    project_dir: Path,
    relative_path: Path,
    *,
    kind: str,
    media_type: str,
    run_id: str,
    logical_id: str,
) -> ArtifactRecord:
    path = project_dir / relative_path
    digest = _sha256(path)
    safe_kind = kind.replace("_", "-")
    return ArtifactRecord(
        artifact_id=f"artifact_{safe_kind}_{digest[:16]}",
        kind=kind,
        relative_path=relative_path.as_posix(),
        media_type=media_type,
        created_at=_utc_now(),
        run_id=run_id,
        logical_id=logical_id,
        size_bytes=path.stat().st_size,
        sha256=digest,
    )


@dataclass(frozen=True)
class DrawingDiscoveryBatch:
    fragment_count: int = 0
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


class DrawingAnalyzer:
    key = "drawing"
    version = DRAWING_PIPELINE_VERSION
    supported_inputs = ("drawing", "fusion")

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_pages: int = 50,
        pipeline: Callable[..., DrawingPipelineResult] = execute_2d_pipeline,
        capability_probe: Callable[
            [set[str] | None], dict[str, Any]
        ] = pipeline_capability,
    ) -> None:
        self.enabled = enabled
        self.max_pages = max_pages
        self.pipeline = pipeline
        self.capability_probe = capability_probe

    @property
    def cache_identity(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "max_pages": self.max_pages,
                "semantic_interpreter": "hermes_agent_event_loop",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def capability(self, context: AnalyzerContext) -> Capability:
        drawing_inputs = [item for item in context.inputs if item.kind == "drawing"]
        suffixes = {
            Path(item.relative_path or item.source_name).suffix.lower()
            for item in drawing_inputs
        }
        details = self.capability_probe(suffixes)
        details = {
            **details,
            "applicable": bool(drawing_inputs),
            "semantic_interpreter": "hermes_agent_event_loop",
        }
        if not self.enabled:
            return Capability(
                self.key,
                CapabilityStatus.DISABLED,
                "Drawing analysis is disabled by configuration.",
                "disabled",
                details,
            )
        if drawing_inputs and not details.get("available"):
            return Capability(
                self.key,
                CapabilityStatus.DEPENDENCY_MISSING,
                "Drawing analysis dependencies are unavailable.",
                "dependency_missing",
                details,
            )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "Drawing observation extraction is available.",
            details=details,
        )

    @staticmethod
    def _emit(context: AnalyzerContext, event: WorkerEvent) -> None:
        if context.event_sink is not None:
            context.event_sink(event)

    def discover_input(
        self,
        context: AnalyzerContext,
        input_record: InputRecord,
        cancellation: CancellationToken,
    ) -> DrawingDiscoveryBatch:
        cancellation.raise_if_cancelled()
        if input_record.kind != "drawing":
            raise DFMError(
                "drawing_input_required",
                "Drawing discovery requires a drawing InputRecord.",
                {"input_id": input_record.input_id, "kind": input_record.kind},
            )
        path = (context.project_dir / input_record.relative_path).resolve()
        try:
            result = self.pipeline(
                str(path),
                max_pages=self.max_pages,
            )
        except DrawingPipelineError as exc:
            raise DFMError(exc.code, str(exc), exc.details) from exc
        except Exception as exc:
            raise DFMError(
                "drawing_pipeline_failed",
                f"Drawing pipeline failed: {exc}",
                {"error_type": type(exc).__name__, "input_id": input_record.input_id},
            ) from exc
        cancellation.raise_if_cancelled()

        if context.run_id:
            relative_dir = Path("runs") / context.run_id / "artifacts"
        else:
            relative_dir = Path("discovery") / "drawing" / input_record.sha256[:16]
        output_dir = context.project_dir / relative_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"drawing_{input_record.sha256[:16]}"

        raw_artifact = None
        artifacts: list[ArtifactRecord] = []
        if result.raw_text:
            raw_path = output_dir / f"{base_name}_raw_ocr.txt"
            raw_path.write_text(result.raw_text, encoding="utf-8", newline="\n")
            raw_artifact = _artifact(
                context.project_dir,
                relative_dir / raw_path.name,
                kind="drawing_raw_text",
                media_type="text/plain",
                run_id=context.run_id,
                logical_id=(
                    f"drawing-raw:{input_record.input_id}:"
                    f"{self.version}:{self.cache_identity}"
                ),
            )
            artifacts.append(raw_artifact)

        fragments = []
        for sequence, fragment in enumerate(result.fragments, start=1):
            identity = json.dumps(
                {
                    "input_sha256": input_record.sha256,
                    "sequence": sequence,
                    "page": fragment.page,
                    "bbox": fragment.bbox,
                    "text": fragment.text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            fragment_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            fragments.append({
                "fragment_id": f"fragment.drawing.{fragment_hash[:20]}",
                "input_id": input_record.input_id,
                "input_sha256": input_record.sha256,
                "sequence": sequence,
                **fragment.to_dict(),
            })

        fragments_path = output_dir / f"{base_name}_ocr_fragments.jsonl"
        fragments_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in fragments
            ),
            encoding="utf-8",
            newline="\n",
        )
        fragments_artifact = _artifact(
            context.project_dir,
            relative_dir / fragments_path.name,
            kind="drawing_ocr_fragments",
            media_type="application/x-ndjson",
            run_id=context.run_id,
            logical_id=(
                f"drawing-fragments:{input_record.input_id}:"
                f"{self.version}:{self.cache_identity}"
            ),
        )
        artifacts.append(fragments_artifact)

        diagnostic = {
            "input_id": input_record.input_id,
            "input_sha256": input_record.sha256,
            "provider": result.provider,
            "provider_version": result.provider_version,
            "ocr_fragment_count": len(fragments),
            "interpretation_status": "pending_agent",
            **result.diagnostics,
        }
        diagnostics_path = output_dir / f"{base_name}_diagnostics.json"
        diagnostics_path.write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifacts.append(
            _artifact(
                context.project_dir,
                relative_dir / diagnostics_path.name,
                kind="drawing_diagnostics",
                media_type="application/json",
                run_id=context.run_id,
                logical_id=(
                    f"drawing-diagnostics:{input_record.input_id}:"
                    f"{self.version}:{self.cache_identity}"
                ),
            )
        )
        return DrawingDiscoveryBatch(len(fragments), artifacts, [diagnostic])

    def run(
        self,
        context: AnalyzerContext,
        cancellation: CancellationToken,
    ) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        drawing_inputs = [item for item in context.inputs if item.kind == "drawing"]
        if not drawing_inputs:
            raise DFMError("input_required", "A drawing input is required.")
        capability = self.capability(context)
        if capability.status is not CapabilityStatus.AVAILABLE:
            raise DFMError(
                capability.error_code or "dependency_missing",
                capability.reason,
                capability.details,
            )

        self._emit(
            context,
            WorkerEvent(
                WORKER_SCHEMA_VERSION,
                "progress",
                stage="drawing_analysis",
                percent=1,
            ),
        )
        artifacts: list[ArtifactRecord] = []
        try:
            for index, input_record in enumerate(drawing_inputs, start=1):
                batch = self.discover_input(context, input_record, cancellation)
                artifacts.extend(batch.artifacts)
                for artifact in batch.artifacts:
                    self._emit(
                        context,
                        WorkerEvent(
                            WORKER_SCHEMA_VERSION,
                            "artifact",
                            kind=artifact.kind,
                            path=Path(artifact.relative_path).name,
                        ),
                    )
                self._emit(
                    context,
                    WorkerEvent(
                        WORKER_SCHEMA_VERSION,
                        "progress",
                        stage="drawing_analysis",
                        percent=min(99, int(index / len(drawing_inputs) * 100)),
                    ),
                )
        except DFMError as exc:
            self._emit(
                context,
                WorkerEvent(
                    WORKER_SCHEMA_VERSION,
                    "error",
                    code=exc.code,
                    message=exc.message,
                ),
            )
            raise
        self._emit(
            context,
            WorkerEvent(
                WORKER_SCHEMA_VERSION,
                "completed",
                stage="drawing_analysis",
                percent=100,
            ),
        )
        return artifacts
