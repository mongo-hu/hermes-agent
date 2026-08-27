"""Adapter from the isolated 2D pipeline to formal DFM observations."""

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
    ObservationRecord,
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
    observations: list[ObservationRecord] = field(default_factory=list)
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
        model_name: str = "",
        base_url: str = "",
        timeout_seconds: int = 60,
        pipeline: Callable[..., DrawingPipelineResult] = execute_2d_pipeline,
        capability_probe: Callable[
            [set[str] | None], dict[str, Any]
        ] = pipeline_capability,
    ) -> None:
        self.enabled = enabled
        self.max_pages = max_pages
        self.model_name = model_name
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.pipeline = pipeline
        self.capability_probe = capability_probe

    @property
    def cache_identity(self) -> str:
        payload = json.dumps(
            {
                "version": self.version,
                "model": self.model_name,
                "base_url": self.base_url,
                "max_pages": self.max_pages,
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
            "semantic_model": self.model_name or None,
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

    @staticmethod
    def _source_ref(
        input_record: InputRecord, raw_artifact: ArtifactRecord | None, candidate
    ) -> str:
        base = (
            f"artifact:{raw_artifact.artifact_id}"
            if raw_artifact is not None
            else f"input:{input_record.input_id}"
        )
        qualifiers = []
        if candidate.page is not None:
            qualifiers.append(f"page={candidate.page}")
        if candidate.bbox:
            qualifiers.append(
                "bbox=" + ",".join(f"{value:g}" for value in candidate.bbox)
            )
        return base + ("#" + "&".join(qualifiers) if qualifiers else "")

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
                model_name=self.model_name,
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
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

        observations: list[ObservationRecord] = []
        counts: dict[str, int] = {}
        for candidate in result.candidates:
            counts[candidate.kind] = counts.get(candidate.kind, 0) + 1
            sequence = counts[candidate.kind]
            observation_id = (
                f"observation.drawing.{input_record.sha256[:16]}."
                f"{candidate.kind}.{sequence:03d}"
            )
            observations.append(
                ObservationRecord(
                    observation_id=observation_id,
                    input_id=input_record.input_id,
                    kind=candidate.kind,
                    value=candidate.value,
                    source_refs=[
                        self._source_ref(input_record, raw_artifact, candidate)
                    ],
                    confidence=max(0.0, min(float(candidate.confidence), 1.0)),
                    status="candidate",
                    unit=candidate.unit,
                    provenance={
                        "provider": result.provider,
                        "provider_version": result.provider_version,
                        "pipeline_config": self.cache_identity,
                        "input_sha256": input_record.sha256,
                        "page": candidate.page,
                        "bbox": candidate.bbox,
                        "original_text": candidate.original_text[:500],
                        "feature_kind": candidate.feature_kind,
                        "region_role": candidate.region_role,
                    },
                )
            )

        observations_path = output_dir / f"{base_name}_observations.jsonl"
        observations_path.write_text(
            "".join(
                json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                for item in observations
            ),
            encoding="utf-8",
            newline="\n",
        )
        observations_artifact = _artifact(
            context.project_dir,
            relative_dir / observations_path.name,
            kind="drawing_observations",
            media_type="application/x-ndjson",
            run_id=context.run_id,
            logical_id=(
                f"drawing-observations:{input_record.input_id}:"
                f"{self.version}:{self.cache_identity}"
            ),
        )
        artifacts.append(observations_artifact)

        diagnostic = {
            "input_id": input_record.input_id,
            "input_sha256": input_record.sha256,
            "provider": result.provider,
            "provider_version": result.provider_version,
            "observation_count": len(observations),
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
        return DrawingDiscoveryBatch(observations, artifacts, [diagnostic])

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
