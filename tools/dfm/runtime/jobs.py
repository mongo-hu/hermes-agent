"""Non-blocking, manifest-backed DFM run lifecycle."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import os
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from ..analyzers.base import AnalyzerContext, CancellationToken
from ..analyzers.registry import AnalyzerRegistry
from ..config import DFMConfig
from ..contracts import (
    ArtifactRecord,
    PlanRecord,
    ProjectManifest,
    RunRecord,
    RunStatus,
    STAGE_COMPLETE,
    STAGE_EVIDENCE_RENDER,
    STAGE_OBJECTIVE_READY,
    STAGE_REPORT_MATERIALIZE,
    STAGE_RULE_EVALUATION,
    WorkerEvent,
    ensure_run_transition,
)
from ..errors import DFMError
from ..evidence import FieldEvidenceEngine
from ..evaluation import EvaluationEngine
from ..findings import materialize_evaluated_findings
from ..project.manifest import ManifestStore
from ..project.workspace import DFMWorkspace
from ..reporting.result_assembler import materialize_result_reports
from ..viewer import materialize_viewer_manifest
from .objective_cache import ObjectiveOperationCache


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobManager:
    def __init__(
        self,
        workspace: DFMWorkspace,
        registry: AnalyzerRegistry,
        config: DFMConfig,
        *,
        executor: Any | None = None,
        reconcile: bool = True,
    ) -> None:
        self.workspace = workspace
        self.registry = registry
        self.config = config
        self._executor = executor or ThreadPoolExecutor(
            max_workers=config.max_concurrent_runs,
            thread_name_prefix="dfm",
        )
        self._futures: dict[str, Future] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._listeners: dict[str, Callable[[RunRecord], None]] = {}
        self._lock = RLock()
        self.runtime_id = f"runtime_{uuid4().hex[:16]}"
        self.evaluation_engine = EvaluationEngine()
        self.field_evidence_engine = FieldEvidenceEngine()
        self.objective_cache = ObjectiveOperationCache()
        if reconcile:
            self.reconcile_incomplete_runs()

    def _store(self, project_id: str) -> ManifestStore:
        return ManifestStore(self.workspace.project_dir(project_id))

    @staticmethod
    def _find_run(manifest: ProjectManifest, run_id: str) -> RunRecord:
        for run in manifest.runs:
            if run.run_id == run_id:
                return run
        raise DFMError("run_not_found", "DFM run was not found.", {"run_id": run_id})

    def status(self, project_id: str, run_id: str) -> RunRecord:
        return self._find_run(self._store(project_id).load(), run_id)

    def start(
        self,
        project_id: str,
        analyzer_key: str,
        *,
        plan: PlanRecord | None = None,
        idempotency_key: str | None = None,
        on_update: Callable[[RunRecord], None] | None = None,
    ) -> RunRecord:
        store = self._store(project_id)
        manifest = store.load()
        if idempotency_key:
            for existing in manifest.runs:
                if existing.idempotency_key == idempotency_key:
                    return existing
        analyzer = self.registry.get(analyzer_key)
        context = AnalyzerContext(
            project_id,
            self.workspace.project_dir(project_id),
            manifest.input_mode,
            manifest.inputs,
            plan=plan,
        )
        capability = analyzer.capability(context)
        if capability.status.value != "available":
            raise DFMError(
                capability.error_code or capability.status.value,
                capability.reason,
                capability.details,
            )

        with self._lock:
            active = sum(not future.done() for future in self._futures.values())
            run_id = f"run_{uuid4().hex[:16]}"
            now = _utc_now()
            status = RunStatus.QUEUED if active < self.config.max_concurrent_runs else RunStatus.BLOCKED
            error = None
            if status is RunStatus.BLOCKED:
                error = {"code": "concurrency_limit", "message": "DFM concurrency limit reached."}
            run = RunRecord(
                run_id,
                analyzer.key,
                analyzer.version,
                status,
                now,
                now,
                error=error,
                idempotency_key=idempotency_key,
                owner_pid=os.getpid(),
                runtime_id=self.runtime_id,
                plan_id=plan.plan_id if plan else None,
                plan_snapshot=plan.to_dict() if plan else None,
                stage="queued",
                progress_percent=0,
                heartbeat_at=now,
                event_log_path=f"runs/{run_id}/events.jsonl",
                worker_stdout_path=f"runs/{run_id}/worker.stdout.log",
                worker_stderr_path=f"runs/{run_id}/worker.stderr.log",
            )
            store.update(lambda current: replace(current, runs=[*current.runs, run], updated_at=now))
            if status is RunStatus.BLOCKED:
                return run
            token = CancellationToken()
            self._tokens[run_id] = token
            if on_update is not None:
                self._listeners[run_id] = on_update
            try:
                future = self._executor.submit(self._execute, project_id, run_id, analyzer, token)
            except Exception:
                self._tokens.pop(run_id, None)
                self._mark_failure(
                    project_id,
                    run_id,
                    RunStatus.FAILED,
                    "runtime_submit_failed",
                    "The DFM runtime could not submit the run.",
                )
                self._listeners.pop(run_id, None)
                return self.status(project_id, run_id)
            self._futures[run_id] = future
            future.add_done_callback(lambda _future, rid=run_id: self._forget_future(rid))
            return run

    def _forget_future(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)

    @staticmethod
    def _plan_input_sha256(manifest: ProjectManifest, plan: PlanRecord | None) -> str:
        if plan is None:
            return ""
        by_id = {item.input_id: item.sha256 for item in manifest.inputs}
        hashes = {
            plan.input_hashes.get(input_id) or by_id.get(input_id, "")
            for input_id in plan.input_ids
        }
        hashes.discard("")
        return next(iter(hashes)) if len(hashes) == 1 else ""

    def _replace_run(self, project_id: str, run_id: str, transform) -> RunRecord:
        selected: RunRecord | None = None

        def update(manifest: ProjectManifest) -> ProjectManifest:
            nonlocal selected
            runs = []
            for run in manifest.runs:
                if run.run_id == run_id:
                    selected = transform(run)
                    if selected.status is not run.status:
                        ensure_run_transition(run.status, selected.status)
                    runs.append(selected)
                else:
                    runs.append(run)
            if selected is None:
                raise DFMError("run_not_found", "DFM run was not found.", {"run_id": run_id})
            return replace(manifest, runs=runs, updated_at=_utc_now())

        self._store(project_id).update(update)
        assert selected is not None
        return selected

    def _execute(self, project_id: str, run_id: str, analyzer, token: CancellationToken) -> None:
        try:
            current = self.status(project_id, run_id)
            if current.status is not RunStatus.QUEUED:
                return
            self._replace_run(
                project_id,
                run_id,
                lambda run: replace(
                    run,
                    status=RunStatus.RUNNING,
                    updated_at=_utc_now(),
                    heartbeat_at=_utc_now(),
                    stage="starting",
                    progress_percent=max(run.progress_percent, 1),
                ),
            )
            manifest = self._store(project_id).load()
            persisted_run = self._find_run(manifest, run_id)
            plan = (
                PlanRecord.from_dict(persisted_run.plan_snapshot)
                if persisted_run.plan_snapshot
                else None
            )
            context = AnalyzerContext(
                project_id,
                self.workspace.project_dir(project_id),
                manifest.input_mode,
                manifest.inputs,
                run_id,
                plan,
                lambda event: self._record_event(project_id, run_id, event),
            )
            input_sha256 = self._plan_input_sha256(manifest, plan)
            artifacts = (
                self.objective_cache.restore(
                    context.project_dir,
                    run_id,
                    plan,
                    input_sha256=input_sha256,
                    analyzer_key=analyzer.key,
                    analyzer_version=analyzer.version,
                )
                if plan is not None and input_sha256
                else None
            )
            if artifacts is None:
                artifacts = analyzer.run(context, token)
            else:
                self._advance_stage(project_id, run_id, STAGE_OBJECTIVE_READY, 70)
            checked = [self._validate_artifact(context.project_dir, item) for item in artifacts]
            if plan is not None and input_sha256:
                self.objective_cache.publish(
                    context.project_dir,
                    run_id,
                    plan,
                    checked,
                    input_sha256=input_sha256,
                    analyzer_key=analyzer.key,
                    analyzer_version=analyzer.version,
                )
            if plan is not None and any(item.kind == "measurements" for item in checked):
                self._advance_stage(project_id, run_id, STAGE_RULE_EVALUATION, 75)
                evaluation_artifact = self.evaluation_engine.materialize(
                    context.project_dir,
                    run_id,
                    plan,
                    checked,
                )
                checked.append(
                    self._validate_artifact(context.project_dir, evaluation_artifact)
                )
                if any(item.kind == "scalar_field" for item in checked):
                    self._advance_stage(project_id, run_id, STAGE_EVIDENCE_RENDER, 82)
                    evidence_artifacts = self.field_evidence_engine.materialize(
                        context.project_dir,
                        run_id,
                        checked,
                        max_images=self.config.max_evidence_findings,
                    )
                    checked.extend(
                        self._validate_artifact(context.project_dir, item)
                        for item in evidence_artifacts
                    )
                self._advance_stage(project_id, run_id, STAGE_REPORT_MATERIALIZE, 94)
                report_artifacts = materialize_result_reports(
                    context.project_dir,
                    run_id,
                    plan,
                    checked,
                )
                checked.extend(
                    self._validate_artifact(context.project_dir, item)
                    for item in report_artifacts
                )
                viewer_artifact = materialize_viewer_manifest(
                    context.project_dir,
                    run_id,
                    plan,
                    checked,
                )
                if viewer_artifact is not None:
                    checked.append(
                        self._validate_artifact(context.project_dir, viewer_artifact)
                    )
            self._complete_success(project_id, run_id, checked)
        except DFMError as exc:
            logger.warning(
                "DFM run failed: project_id=%s run_id=%s code=%s",
                project_id,
                run_id,
                exc.code,
                exc_info=True,
            )
            status = RunStatus.CANCELLED if exc.code == "run_cancelled" else RunStatus.FAILED
            self._mark_failure(project_id, run_id, status, exc.code, exc.message)
        except Exception:
            logger.exception(
                "Unexpected DFM analyzer failure: project_id=%s run_id=%s",
                project_id,
                run_id,
            )
            self._mark_failure(
                project_id,
                run_id,
                RunStatus.FAILED,
                "analyzer_failed",
                "The DFM analyzer failed. Run diagnostics for details.",
            )
        finally:
            with self._lock:
                self._tokens.pop(run_id, None)
                self._listeners.pop(run_id, None)

    def _mark_failure(self, project_id: str, run_id: str, status: RunStatus, code: str, message: str) -> None:
        try:
            updated = self._replace_run(
                project_id,
                run_id,
                lambda run: run if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING} else replace(
                    run,
                    status=status,
                    updated_at=_utc_now(),
                    heartbeat_at=_utc_now(),
                    stage="cancelled" if status is RunStatus.CANCELLED else "failed",
                    error={"code": code, "message": message},
                ),
            )
            self._notify(run_id, updated)
        except DFMError:
            return

    def _advance_stage(
        self, project_id: str, run_id: str, stage: str, percent: int
    ) -> None:
        now = _utc_now()
        updated = self._replace_run(
            project_id,
            run_id,
            lambda run: replace(
                run,
                stage=stage,
                progress_percent=max(run.progress_percent, percent),
                heartbeat_at=now,
                updated_at=now,
            ),
        )
        self._notify(run_id, updated)

    def _complete_success(
        self,
        project_id: str,
        run_id: str,
        artifacts: list[ArtifactRecord],
    ) -> None:
        now = _utc_now()

        def complete(current: ProjectManifest) -> ProjectManifest:
            runs = []
            found = False
            for run in current.runs:
                if run.run_id != run_id:
                    runs.append(run)
                    continue
                found = True
                ensure_run_transition(run.status, RunStatus.SUCCEEDED)
                combined = {item.relative_path: item for item in run.artifacts}
                combined.update({item.relative_path: item for item in artifacts})
                runs.append(
                    replace(
                        run,
                        status=RunStatus.SUCCEEDED,
                        updated_at=now,
                        heartbeat_at=now,
                        stage=STAGE_COMPLETE,
                        progress_percent=100,
                        artifacts=list(combined.values()),
                    )
                )
            if not found:
                raise DFMError("run_not_found", "DFM run was not found.", {"run_id": run_id})
            known_paths = {item.relative_path for item in current.artifacts}
            findings = materialize_evaluated_findings(
                self.workspace.project_dir(project_id), artifacts
            )
            finding_ids = {item.finding_id for item in findings}
            return replace(
                current,
                runs=runs,
                artifacts=[
                    *current.artifacts,
                    *[
                        item
                        for item in artifacts
                        if item.relative_path not in known_paths
                    ],
                ],
                findings=[item for item in current.findings if item.finding_id not in finding_ids] + findings,
                updated_at=now,
            )

        self._store(project_id).update(complete)
        self._notify(run_id, self.status(project_id, run_id))

    def _record_event(self, project_id: str, run_id: str, event: WorkerEvent) -> None:
        now = _utc_now()
        project_dir = self.workspace.project_dir(project_id)
        event_path = project_dir / "runs" / run_id / "events.jsonl"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

        artifact = self._artifact_from_event(project_dir, run_id, event, now)

        def update(run: RunRecord) -> RunRecord:
            if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                return run
            artifacts = list(run.artifacts)
            if artifact is not None and all(
                item.relative_path != artifact.relative_path for item in artifacts
            ):
                artifacts.append(artifact)
            percent = run.progress_percent
            stage = run.stage
            if event.type == "progress":
                percent = max(percent, int(event.percent or 0))
                stage = event.stage or stage
            return replace(
                run,
                artifacts=artifacts,
                progress_percent=percent,
                stage=stage,
                heartbeat_at=now,
                updated_at=now,
                external_job_id=event.external_job_id or run.external_job_id,
            )

        updated = self._replace_run(project_id, run_id, update)
        self._notify(run_id, updated)

    def _notify(self, run_id: str, run: RunRecord) -> None:
        listener = self._listeners.get(run_id)
        if listener is None:
            return
        try:
            listener(run)
        except Exception:
            logger.debug("DFM run update listener failed: run_id=%s", run_id, exc_info=True)

    @staticmethod
    def _artifact_from_event(
        project_dir: Path,
        run_id: str,
        event: WorkerEvent,
        created_at: str,
    ) -> ArtifactRecord | None:
        if event.type != "artifact" or not event.path:
            return None
        relative = Path("runs") / run_id / "artifacts" / Path(event.path)
        resolved = (project_dir / relative).resolve()
        artifact_root = (project_dir / "runs" / run_id / "artifacts").resolve()
        if (
            Path(event.path).is_absolute()
            or not resolved.is_relative_to(artifact_root)
            or not resolved.is_file()
        ):
            logger.warning(
                "Ignoring invalid incremental DFM artifact: run_id=%s path=%s",
                run_id,
                event.path,
            )
            return None
        media_type = {
            ".json": "application/json",
            ".md": "text/markdown",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".png": "image/png",
            ".step": "model/step",
            ".stp": "model/step",
        }.get(resolved.suffix.lower(), "application/octet-stream")
        digest = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:16]
        kind = {
            "image": "evidence_image",
            "step": "highlighted_step",
        }.get(event.kind or "", event.kind or "artifact")
        return ArtifactRecord(
            f"artifact_{digest}",
            kind,
            relative.as_posix(),
            media_type,
            created_at,
        )

    @staticmethod
    def _validate_artifact(project_dir: Path, artifact: ArtifactRecord) -> ArtifactRecord:
        relative = Path(artifact.relative_path)
        resolved = (project_dir / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(project_dir.resolve()) or not resolved.is_file():
            raise DFMError(
                "artifact_invalid",
                "Analyzer returned an invalid artifact path.",
                {"path": artifact.relative_path},
            )
        size_bytes = resolved.stat().st_size
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        if artifact.size_bytes not in {0, size_bytes} or (
            artifact.sha256 and artifact.sha256 != sha256
        ):
            raise DFMError(
                "artifact_invalid",
                "Analyzer artifact size or content hash does not match its manifest.",
                {"path": artifact.relative_path},
            )
        parts = relative.parts
        run_id = artifact.run_id
        if len(parts) >= 2 and parts[0] == "runs":
            path_run_id = parts[1]
            if run_id and run_id != path_run_id:
                raise DFMError(
                    "artifact_invalid",
                    "Analyzer artifact belongs to a different run.",
                    {"path": artifact.relative_path},
                )
            run_id = path_run_id
        return replace(
            artifact,
            run_id=run_id,
            logical_id=artifact.logical_id or artifact.artifact_id,
            size_bytes=size_bytes,
            sha256=sha256,
        )

    def cancel(self, project_id: str, run_id: str) -> RunRecord:
        run = self.status(project_id, run_id)
        if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return run
        with self._lock:
            token = self._tokens.get(run_id)
            if token:
                token.cancel()
            future = self._futures.get(run_id)
            if run.status is RunStatus.QUEUED and future and future.cancel():
                return self._replace_run(
                    project_id,
                    run_id,
                    lambda item: replace(item, status=RunStatus.CANCELLED, updated_at=_utc_now()),
                )
        return self.status(project_id, run_id)

    def result(self, project_id: str, run_id: str) -> RunRecord:
        run = self.status(project_id, run_id)
        if run.status is not RunStatus.SUCCEEDED:
            raise DFMError(
                "result_not_ready",
                "DFM run result is not ready.",
                {"run_id": run_id, "status": run.status.value},
            )
        return run

    def reconcile_incomplete_runs(self) -> None:
        if not self.workspace.projects_dir.exists():
            return
        for project_dir in self.workspace.projects_dir.glob("dfm_*"):
            store = ManifestStore(project_dir)
            try:
                manifest = store.load()
            except DFMError:
                continue
            if not any(
                run.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                and not self._pid_is_alive(run.owner_pid)
                for run in manifest.runs
            ):
                continue
            now = _utc_now()
            store.update(
                lambda current: replace(
                    current,
                    runs=[
                        replace(
                            run,
                            status=RunStatus.BLOCKED,
                            updated_at=now,
                            error={
                                "code": "runtime_restarted",
                                "message": "Run was interrupted by a runtime restart.",
                            },
                        )
                        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                        and not self._pid_is_alive(run.owner_pid)
                        else run
                        for run in current.runs
                    ],
                    updated_at=now,
                )
            )

    @staticmethod
    def _pid_is_alive(pid: int | None) -> bool:
        if pid is None:
            return False
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def shutdown(self) -> None:
        with self._lock:
            for token in self._tokens.values():
                token.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
