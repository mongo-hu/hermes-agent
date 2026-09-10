"""Reference STEP analyzer backed by the isolated PythonOCC worker."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable

from ..contracts import (
    OBJECTIVE_SCHEMA_VERSION,
    WORKER_SCHEMA_VERSION,
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    LocalObjectiveWorkerRequest,
    ObjectiveResultManifest,
    ObjectiveTaskRequest,
    WorkerEvent,
)
from ..errors import DFMError
from ..runtime.process import ProcessRunner
from ..workers.step_worker import WORKER_VERSION
from .base import AnalyzerContext, CancellationToken
from .objective_result import validate_objective_result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _module_available(python_executable: str, module: str) -> bool:
    if Path(python_executable).resolve() == Path(sys.executable).resolve():
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, AttributeError, ValueError):
            return False
    try:
        completed = subprocess.run(
            [python_executable, "-c", f"import {module}"],
            check=False,
            capture_output=True,
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def dependency_statuses(python_executable: str) -> dict[str, bool]:
    return {
        "pythonocc-core": _module_available(python_executable, "OCC"),
        "vtk": _module_available(python_executable, "vtk"),
    }


def _dependency_available(python_executable: str) -> bool:
    return all(dependency_statuses(python_executable).values())


class StepAnalyzer:
    key = "step"
    version = WORKER_VERSION
    supported_inputs = ("step", "fusion")

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        dependency_probe: Callable[[], bool] | None = None,
        python_executable: str | None = None,
        timeout_seconds: float = 900,
    ) -> None:
        self.runner = runner or ProcessRunner()
        self.python_executable = python_executable or sys.executable
        self.dependency_probe = dependency_probe or (
            lambda: _dependency_available(self.python_executable)
        )
        self._dependency_status: bool | None = None
        self._dependency_lock = threading.Lock()
        self.timeout_seconds = timeout_seconds

    def capability(self, context: AnalyzerContext) -> Capability:
        if self._dependency_status is None:
            with self._dependency_lock:
                if self._dependency_status is None:
                    self._dependency_status = bool(self.dependency_probe())
        if not self._dependency_status:
            return Capability(
                self.key,
                CapabilityStatus.DEPENDENCY_MISSING,
                "pythonocc-core/OpenCascade and VTK are required by the STEP analyzer.",
                "dependency_missing",
                {
                    "dependencies": ["pythonocc-core", "vtk"],
                    "install_extra": "hermes-agent[dfm]",
                    "worker_version": self.version,
                },
            )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "The isolated PythonOCC STEP reference analyzer is available.",
            details={
                "worker_version": self.version,
                "backend_id": "pythonocc_reference",
                "role": "reference_regression",
                "production_ready": False,
                "production_target": "external_occt_cpp",
                "supported_processes": ["die_casting", "injection"],
                "format_ids": ["step"],
                "representation": "brep",
            },
        )

    def run(
        self,
        context: AnalyzerContext,
        cancellation: CancellationToken,
    ) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        capability = self.capability(context)
        if capability.status is not CapabilityStatus.AVAILABLE:
            raise DFMError(
                capability.error_code or "dependency_missing",
                capability.reason,
                capability.details,
            )
        if context.plan is None:
            raise DFMError(
                "plan_required", "A persisted DFM execution plan is required."
            )
        if context.plan.process not in {"injection", "die_casting"}:
            raise DFMError(
                "unsupported_capability",
                f"DFM process is not supported: {context.plan.process}",
                {"supported_processes": ["die_casting", "injection"]},
            )
        input_record = next(
            (
                item
                for item in context.inputs
                if item.input_id in context.plan.input_ids and item.kind == "step"
            ),
            None,
        )
        if input_record is None:
            raise DFMError(
                "input_required", "The DFM plan does not reference a STEP input."
            )

        run_dir = context.project_dir / "runs" / context.run_id
        output_dir = run_dir / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        task = ObjectiveTaskRequest(
            schema_version=OBJECTIVE_SCHEMA_VERSION,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            input_format=input_record.format_id or "step",
            process=context.plan.process,
            scope_id=context.plan.scope_id,
            scope_version=context.plan.scope_version,
            operations=context.plan.operations,
            regions=[
                item
                for item in context.plan.regions
                if item.input_sha256 == input_record.sha256
                and any(
                    item.region_id in operation.region_refs
                    for operation in context.plan.operations
                )
            ],
        )
        request = LocalObjectiveWorkerRequest(
            schema_version=WORKER_SCHEMA_VERSION,
            backend_version=self.version,
            input_path=str(
                (context.project_dir / input_record.relative_path).resolve()
            ),
            output_dir=str(output_dir.resolve()),
            task=task,
        )
        request_path = run_dir / "request.json"
        request_path.write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        events: list[WorkerEvent] = []

        def handle_event(event: WorkerEvent) -> None:
            events.append(event)
            if context.event_sink is not None:
                context.event_sink(event)

        process_result = self.runner.run(
            [
                self.python_executable,
                "-m",
                "tools.dfm.workers.step_worker",
                "--request",
                str(request_path),
            ],
            Path(__file__).resolve().parents[3],
            self.timeout_seconds,
            cancellation,
            handle_event,
            run_dir / "worker.stdout.log",
            run_dir / "worker.stderr.log",
        )
        if process_result.returncode != 0:
            error = next(
                (event for event in reversed(events) if event.type == "error"), None
            )
            raise DFMError(
                error.code if error and error.code else "analyzer_failed",
                error.message
                if error and error.message
                else "The STEP analyzer failed.",
            )
        completed = [event for event in events if event.type == "completed"]
        if len(completed) != 1 or not completed[0].path:
            raise DFMError(
                "objective_result_invalid",
                "The STEP worker did not emit exactly one completion result.",
            )
        result_path = self._contained_file(output_dir, completed[0].path)
        try:
            result = ObjectiveResultManifest.from_dict(
                json.loads(result_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DFMError(
                "objective_result_invalid",
                "The STEP worker result could not be loaded.",
            ) from exc
        if (
            result.schema_version != OBJECTIVE_SCHEMA_VERSION
            or result.producer_version != self.version
            or result.run_id != context.run_id
            or result.process != context.plan.process
            or result.scope_id != context.plan.scope_id
            or result.scope_version != context.plan.scope_version
            or result.input_sha256 != input_record.sha256
        ):
            raise DFMError(
                "objective_result_invalid",
                "The STEP worker result does not match its persisted plan.",
            )
        measurement_artifacts = [
            item for item in result.artifacts if item.kind == "measurements"
        ]
        if len(measurement_artifacts) != 1:
            raise DFMError(
                "objective_result_invalid",
                "The STEP worker result must reference exactly one measurements artifact.",
            )

        artifacts: list[ArtifactRecord] = []
        for item in result.artifacts:
            path = self._contained_file(output_dir, item.filename)
            if path.stat().st_size != item.size_bytes or self._sha256(path) != item.sha256:
                raise DFMError(
                    "objective_artifact_invalid",
                    "The local objective artifact does not match its manifest.",
                    {"artifact_id": item.artifact_id},
                )
            artifacts.append(
                ArtifactRecord(
                    item.artifact_id,
                    item.kind,
                    path.relative_to(context.project_dir).as_posix(),
                    item.media_type,
                    _utc_now(),
                    run_id=context.run_id,
                    logical_id=item.artifact_id,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                )
            )
        artifacts.append(
            self._artifact_record(
                context.project_dir,
                context.run_id,
                "objective_result_manifest",
                "worker_result",
                result_path,
                "application/json",
            )
        )
        validate_objective_result(
            context.plan.operations,
            context.project_dir,
            artifacts,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            process=context.plan.process,
            scope_id=context.plan.scope_id,
            regions=context.plan.regions,
            error_code="objective_result_invalid",
        )
        return artifacts

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _artifact_record(
        cls,
        project_dir: Path,
        run_id: str,
        artifact_id: str,
        kind: str,
        path: Path,
        media_type: str,
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id,
            kind,
            path.relative_to(project_dir).as_posix(),
            media_type,
            _utc_now(),
            run_id=run_id,
            logical_id=artifact_id,
            size_bytes=path.stat().st_size,
            sha256=cls._sha256(path),
        )

    @staticmethod
    def _contained_file(output_dir: Path, raw_path: str) -> Path:
        relative = Path(raw_path)
        resolved = (output_dir / relative).resolve()
        if (
            not raw_path
            or relative.is_absolute()
            or not resolved.is_relative_to(output_dir.resolve())
            or not resolved.is_file()
        ):
            raise DFMError(
                "objective_artifact_invalid",
                "The STEP worker returned an invalid artifact path.",
                {"path": raw_path},
            )
        return resolved
