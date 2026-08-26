"""Analyzer for 2D drawing observations via the decoupled 2D pipeline."""

import os
from pathlib import Path
from ..contracts import ArtifactRecord, Capability, CapabilityStatus, WorkerEvent
from ..errors import DFMError
from .base import AnalyzerContext, CancellationToken

# Import the decoupled 2D pipeline interface
from ..drawing_pipeline.interface import execute_2d_pipeline

class DrawingAnalyzer:
    key = "drawing"
    version = "1.0.0"
    supported_inputs = ("drawing", "fusion")

    def capability(self, context: AnalyzerContext) -> Capability:
        # Check if there is a drawing input
        drawing_inputs = [item for item in context.inputs if item.kind == "drawing"]
        if not drawing_inputs:
            return Capability(
                self.key,
                CapabilityStatus.BLOCKED,
                "A 2D drawing is required for drawing analysis.",
                "input_missing",
            )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "Drawing analyzer is ready.",
        )

    def run(self, context: AnalyzerContext, cancellation: CancellationToken) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        capability = self.capability(context)
        if capability.status != CapabilityStatus.AVAILABLE:
            raise DFMError("unsupported_capability", capability.reason, capability.details)

        drawing_inputs = [item for item in context.inputs if item.kind == "drawing"]
        input_record = drawing_inputs[0]
        file_path = (context.project_dir / input_record.relative_path).resolve()

        if context.emit_event:
            context.emit_event(WorkerEvent("progress", "started", 0))

        try:
            # Run the decoupled 2D pipeline
            jsonl_result, raw_text = execute_2d_pipeline(str(file_path), quiet=True)
            
            # Save the JSONL string to an artifact (Must remain drawing_observations.jsonl for downstream DB)
            artifact_dir = context.project_dir / "runs" / context.run_id / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            
            output_name = "drawing_observations.jsonl"
            output_path = artifact_dir / output_name
            
            with output_path.open("w", encoding="utf-8") as f:
                f.write(jsonl_result)
                
            # 落盘溯源 raw_text (放到独立的 raw_txt 文件夹，使用源文件名避免覆盖)
            if raw_text:
                raw_txt_dir = context.project_dir / "runs" / context.run_id / "raw_txt"
                raw_txt_dir.mkdir(parents=True, exist_ok=True)
                file_stem = file_path.stem
                raw_path = raw_txt_dir / f"{file_stem}_raw_ocr.txt"
                with raw_path.open("w", encoding="utf-8") as f:
                    f.write(raw_text)

            if context.emit_event:
                context.emit_event(WorkerEvent("artifact", path=output_name, kind="drawing_observations"))
                context.emit_event(WorkerEvent("progress", "completed", 100))
                
            import hashlib
            from datetime import datetime, timezone
            
            size_bytes = output_path.stat().st_size
            digest = hashlib.sha256()
            with output_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            
            relative_path = Path("runs") / context.run_id / "artifacts" / output_name
            artifact = ArtifactRecord(
                f"artifact_{sha256[:16]}",
                "drawing_observations",
                relative_path.as_posix(),
                "application/jsonlines",
                now,
                run_id=context.run_id,
                size_bytes=size_bytes,
                sha256=sha256,
            )
            return [artifact]
            
        except Exception as e:
            if context.emit_event:
                context.emit_event(WorkerEvent("error", "failed", 100))
            raise DFMError("drawing_pipeline_failed", f"2D drawing pipeline failed: {str(e)}")

