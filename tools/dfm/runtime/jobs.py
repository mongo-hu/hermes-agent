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
    STAGE_REPORT_EDITING,
    STAGE_REPORT_QUEUED,
    STAGE_REPORT_RENDERING,
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
from ..reporting.html import materialize_html_runtime
from ..reporting.result_assembler import materialize_result_reports
from ..viewer import materialize_viewer_manifest
from .objective_cache import ObjectiveOperationCache


logger = logging.getLogger(__name__)


_REPORT_JOB_SUFFIX = ":report"


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

    def append_plan(
        self,
        project_id: str,
        run_id: str,
        plan: PlanRecord,
    ) -> RunRecord:
        """Append immutable fragments to an executing Run without changing old input."""

        plan.validate()
        with self._lock:
            run = self.status(project_id, run_id)
            if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING} or (
                run.stage == "sealing"
            ):
                raise DFMError(
                    "run_sealed",
                    "The DFM run no longer accepts plan fragments.",
                    {"run_id": run_id, "status": run.status.value},
                )
            if run.plan_snapshot is None:
                raise DFMError(
                    "plan_fragment_invalid",
                    "Only a planned DFM run can accept plan fragments.",
                    {"run_id": run_id},
                )
            current = PlanRecord.from_dict(run.plan_snapshot)
            if plan.parent_plan_id != run.plan_id:
                raise DFMError(
                    "plan_fragment_invalid",
                    "The appended plan does not continue the Run's current plan.",
                    {
                        "run_id": run_id,
                        "current_plan_id": run.plan_id,
                        "parent_plan_id": plan.parent_plan_id,
                    },
                )
            immutable_identity = (
                "input_mode",
                "process",
                "ontology_snapshot_id",
                "ontology_snapshot_sha256",
                "input_ids",
                "input_hashes",
            )
            changed = [
                name
                for name in immutable_identity
                if getattr(current, name) != getattr(plan, name)
            ]
            if (
                changed
                or current.analyzer_keys != plan.analyzer_keys
                or run.analyzer_key not in plan.analyzer_keys
            ):
                raise DFMError(
                    "plan_fragment_invalid",
                    "A Run cannot change its model, ontology release, process, or analyzer.",
                    {"run_id": run_id, "changed_fields": changed},
                )
            current_fragments = {
                item.fragment_id: item.to_dict() for item in current.plan_fragments
            }
            appended_fragments = {
                item.fragment_id: item.to_dict() for item in plan.plan_fragments
            }
            altered = sorted(
                fragment_id
                for fragment_id, payload in current_fragments.items()
                if appended_fragments.get(fragment_id) != payload
            )
            if altered or not set(current_fragments).issubset(appended_fragments):
                raise DFMError(
                    "plan_fragment_invalid",
                    "Appending a plan must preserve every existing fragment byte-for-byte.",
                    {"run_id": run_id, "fragment_ids": altered},
                )
            if set(current_fragments) == set(appended_fragments):
                raise DFMError(
                    "plan_fragment_invalid",
                    "The replacement plan contains no new immutable fragment.",
                    {"run_id": run_id},
                )
            now = _utc_now()
            appended = self._replace_run(
                project_id,
                run_id,
                lambda item: replace(
                    item,
                    plan_id=plan.plan_id,
                    plan_snapshot=plan.to_dict(),
                    updated_at=now,
                    heartbeat_at=now,
                ),
            )
        self._notify(run_id, appended)
        return appended

    @staticmethod
    def _report_job_key(run_id: str) -> str:
        return f"{run_id}{_REPORT_JOB_SUFFIX}"

    def start_report(
        self,
        project_id: str,
        run_id: str,
        render: Callable[
            [Callable[[str, int], None], CancellationToken], list[ArtifactRecord]
        ],
        *,
        on_update: Callable[[RunRecord], None] | None = None,
    ) -> RunRecord:
        """Queue final report rendering without blocking the caller."""

        job_key = self._report_job_key(run_id)
        with self._lock:
            run = self.status(project_id, run_id)
            if run.status is RunStatus.SUCCEEDED and any(
                item.kind == "report_html" for item in run.artifacts
            ):
                return run
            retryable_report = (
                run.status is RunStatus.FAILED
                and any(
                    item.kind == "report_html_runtime" for item in run.artifacts
                )
                and any(item.kind == "result_manifest" for item in run.artifacts)
            )
            if run.status is not RunStatus.REPORTING and not retryable_report:
                raise DFMError(
                    "result_not_ready",
                    "HTML reports require a report-ready DFM run.",
                    {"run_id": run_id, "status": run.status.value},
                )

            existing = self._futures.get(job_key)
            if existing is not None and not existing.done():
                return run
            if existing is not None:
                self._futures.pop(job_key, None)

            token = CancellationToken()
            self._tokens[job_key] = token
            if on_update is not None:
                self._listeners[job_key] = on_update
            queued = self._replace_run(
                project_id,
                run_id,
                lambda item: replace(
                    item,
                    status=RunStatus.REPORTING,
                    updated_at=_utc_now(),
                    heartbeat_at=_utc_now(),
                    stage=STAGE_REPORT_QUEUED,
                    error=None,
                ),
            )
            try:
                future = self._executor.submit(
                    self._execute_report,
                    project_id,
                    run_id,
                    render,
                    token,
                )
            except Exception:
                self._tokens.pop(job_key, None)
                self._mark_failure(
                    project_id,
                    run_id,
                    RunStatus.FAILED,
                    "runtime_submit_failed",
                    "The DFM runtime could not submit HTML report rendering.",
                    listener_key=job_key,
                )
                self._listeners.pop(job_key, None)
                return self.status(project_id, run_id)
            self._futures[job_key] = future
            future.add_done_callback(
                lambda _future, key=job_key: self._forget_future(key)
            )
            return queued

    def _execute_report(
        self,
        project_id: str,
        run_id: str,
        render: Callable[
            [Callable[[str, int], None], CancellationToken], list[ArtifactRecord]
        ],
        token: CancellationToken,
    ) -> None:
        job_key = self._report_job_key(run_id)
        try:
            token.raise_if_cancelled()
            self._advance_stage(
                project_id,
                run_id,
                STAGE_REPORT_RENDERING,
                99,
                listener_key=job_key,
            )

            def update_progress(stage: str, percent: int) -> None:
                token.raise_if_cancelled()
                self._advance_stage(
                    project_id,
                    run_id,
                    stage,
                    percent,
                    listener_key=job_key,
                )

            artifacts = render(
                update_progress,
                token,
            )
            token.raise_if_cancelled()
            completed = self.attach_artifacts(project_id, run_id, artifacts)
            self._notify(job_key, completed)
        except DFMError as exc:
            status = (
                RunStatus.CANCELLED
                if exc.code == "run_cancelled"
                else RunStatus.FAILED
            )
            self._mark_failure(
                project_id,
                run_id,
                status,
                exc.code,
                exc.message,
                listener_key=job_key,
            )
        except Exception:
            logger.exception(
                "Unexpected DFM report failure: project_id=%s run_id=%s",
                project_id,
                run_id,
            )
            self._mark_failure(
                project_id,
                run_id,
                RunStatus.FAILED,
                "report_generation_failed",
                "The DFM HTML report could not be generated.",
                listener_key=job_key,
            )
        finally:
            with self._lock:
                self._tokens.pop(job_key, None)
                self._listeners.pop(job_key, None)

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
            completed_fragment_refs: set[str] = set()
            checked_by_path: dict[str, ArtifactRecord] = {}
            while True:
                token.raise_if_cancelled()
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
                    frozenset(completed_fragment_refs),
                )
                input_sha256 = self._plan_input_sha256(manifest, plan)
                ready_fragment_refs = (
                    {
                        item.fragment_id
                        for item in plan.active_plan_fragments(statuses={"ready"})
                    }
                    if plan is not None and plan.plan_fragments
                    else set()
                )
                pending_fragment_refs = ready_fragment_refs - completed_fragment_refs
                if plan is not None and plan.plan_fragments and not pending_fragment_refs:
                    with self._lock:
                        latest_run = self.status(project_id, run_id)
                        latest_plan = PlanRecord.from_dict(latest_run.plan_snapshot or {})
                        latest_ready = {
                            item.fragment_id
                            for item in latest_plan.active_plan_fragments(
                                statuses={"ready"}
                            )
                        }
                        if latest_ready - completed_fragment_refs:
                            continue
                        plan = latest_plan
                        manifest = self._store(project_id).load()
                        self._replace_run(
                            project_id,
                            run_id,
                            lambda item: replace(
                                item,
                                stage="sealing",
                                updated_at=_utc_now(),
                                heartbeat_at=_utc_now(),
                            ),
                        )
                    break

                artifacts = (
                    self.objective_cache.restore(
                        context.project_dir,
                        run_id,
                        plan,
                        input_sha256=input_sha256,
                        analyzer_key=analyzer.key,
                        analyzer_version=analyzer.version,
                    )
                    if plan is not None and input_sha256 and not plan.plan_fragments
                    else None
                )
                if artifacts is None:
                    artifacts = analyzer.run(context, token)
                else:
                    self._advance_stage(
                        project_id, run_id, STAGE_OBJECTIVE_READY, 70
                    )
                for item in artifacts:
                    checked = self._validate_artifact(context.project_dir, item)
                    checked_by_path[checked.relative_path] = checked
                if plan is None or not plan.plan_fragments:
                    if plan is not None and input_sha256:
                        self.objective_cache.publish(
                            context.project_dir,
                            run_id,
                            plan,
                            list(checked_by_path.values()),
                            input_sha256=input_sha256,
                            analyzer_key=analyzer.key,
                            analyzer_version=analyzer.version,
                        )
                    break
                completed_fragment_refs.update(pending_fragment_refs)

            checked = list(checked_by_path.values())
            if plan is not None and any(
                item.kind in {"measurements", "geometric_results"}
                for item in checked
            ):
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
                        max_markers_per_image=(
                            self.config.max_evidence_markers_per_image
                        ),
                    )
                    checked.extend(
                        self._validate_artifact(context.project_dir, item)
                        for item in evidence_artifacts
                    )
                if plan.plan_fragments:
                    checked.append(
                        self._validate_artifact(
                            context.project_dir,
                            self._materialize_result_manifest(
                                context.project_dir, run_id, plan, checked
                            ),
                        )
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
                html_runtime = materialize_html_runtime(
                    context.project_dir,
                    run_id,
                    plan,
                    manifest.inputs,
                    checked,
                    semantic_artifacts=manifest.artifacts,
                    observation_refs={
                        observation_id
                        for snapshot in manifest.discovery_snapshots
                        if snapshot.snapshot_id in plan.discovery_snapshot_refs
                        for observation_id in snapshot.observation_refs
                    },
                )
                if html_runtime is not None:
                    checked.append(
                        self._validate_artifact(context.project_dir, html_runtime)
                    )
                viewer_artifact = materialize_viewer_manifest(
                    context.project_dir,
                    run_id,
                    plan,
                    checked,
                    features=manifest.features,
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

    @staticmethod
    def _materialize_result_manifest(
        project_dir: Path,
        run_id: str,
        plan: PlanRecord,
        artifacts: list[ArtifactRecord],
    ) -> ArtifactRecord:
        """Freeze the exact fragment/result/evidence set consumed by reporting."""

        incomplete: list[dict[str, Any]] = []
        result_artifact = next(
            item for item in artifacts if item.kind == "geometric_results"
        )
        try:
            result_payload = json.loads(
                (project_dir / result_artifact.relative_path).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            raise DFMError(
                "geometric_result_invalid",
                "The Geometric result artifact cannot be pinned for reporting.",
            ) from exc
        artifact_by_kind = {item.kind: item for item in artifacts}

        def artifact_payload(kind: str) -> dict[str, Any]:
            artifact = artifact_by_kind.get(kind)
            if artifact is None:
                return {}
            try:
                value = json.loads(
                    (project_dir / artifact.relative_path).read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError) as exc:
                raise DFMError(
                    "result_manifest_input_invalid",
                    f"The {kind} artifact cannot be pinned for reporting.",
                ) from exc
            if not isinstance(value, dict):
                raise DFMError(
                    "result_manifest_input_invalid",
                    f"The {kind} artifact must contain an object.",
                )
            return value

        scene_payload = artifact_payload("render_scene")
        topology_payload = artifact_payload("topology_map")
        render_mesh_snapshot = scene_payload.get("render_mesh_snapshot")
        topology_snapshot = topology_payload.get("topology_snapshot")
        selected_fragments = plan.active_plan_fragments()
        selected_fragment_refs = {item.fragment_id for item in selected_fragments}
        selected_results = [
            item
            for item in result_payload.get("results", [])
            if isinstance(item, dict)
            and item.get("provenance", {}).get("plan_fragment_ref")
            in selected_fragment_refs
        ]
        for item in selected_results:
            if isinstance(item, dict) and item.get("status") != "ok":
                incomplete.append(
                    {
                        key: item.get(key)
                        for key in (
                            "result_id",
                            "snapshot_id",
                            "feature_id",
                            "geometric_id",
                            "status",
                            "reason",
                        )
                    }
                )
        payload = {
            "contract_version": "hermes.dfm.result-manifest/v1",
            "run_id": run_id,
            "plan_id": plan.plan_id,
            "created_at": _utc_now(),
            "plan_fragments": [
                {
                    "fragment_id": item.fragment_id,
                    "status": item.status,
                    "snapshot_id": item.snapshot_id,
                    "source_discovery_snapshot_id": (
                        item.source_discovery_snapshot_id
                    ),
                    "dependency_fingerprint": item.dependency_fingerprint,
                    "capability_version": item.capability_version,
                    "confirmed_fact_refs": list(item.confirmed_fact_refs),
                    "rule_binding_refs": list(item.rule_binding_refs),
                    "review_group_refs": list(item.review_group_refs),
                    "supersedes": item.supersedes,
                    "blocking_reasons": [
                        dict(value) for value in item.blocking_reasons
                    ],
                }
                for item in selected_fragments
            ],
            "artifact_refs": [item.artifact_id for item in artifacts],
            "result_refs": [
                str(item.get("result_id"))
                for item in selected_results
                if isinstance(item, dict) and item.get("result_id")
            ],
            "rule_refs": sorted({
                binding.rule_id
                for binding in plan.rule_bindings
                if binding.binding_id
                in {
                    binding_ref
                    for fragment in selected_fragments
                    for binding_ref in fragment.rule_binding_refs
                }
            }),
            "final_confirmation_refs": sorted({
                value
                for item in selected_fragments
                if item.status == "ready"
                for value in item.review_group_refs
            }),
            "coverage_gaps": [dict(item) for item in plan.coverage_gaps],
            "mesh_refs": [
                item.artifact_id
                for item in artifacts
                if item.kind in {"render_scene", "topology_map"}
            ],
            "render_mesh_snapshot_id": (
                render_mesh_snapshot.get("render_mesh_snapshot_id")
                if isinstance(render_mesh_snapshot, dict)
                else None
            ),
            "topology_snapshot_id": (
                topology_snapshot.get("topology_snapshot_id")
                if isinstance(topology_snapshot, dict)
                else None
            ),
            "evidence_refs": [
                item.artifact_id
                for item in artifacts
                if item.kind
                in {
                    "scalar_field",
                    "evidence",
                    "evidence_geometry",
                    "evidence_records",
                    "evidence_image",
                }
            ],
            "incomplete_results": incomplete,
            "incomplete_fragments": [
                {
                    "fragment_id": item.fragment_id,
                    "status": item.status,
                    "snapshot_id": item.snapshot_id,
                    "rule_binding_refs": list(item.rule_binding_refs),
                    "review_group_refs": list(item.review_group_refs),
                    "blocking_reasons": [
                        dict(value) for value in item.blocking_reasons
                    ],
                }
                for item in selected_fragments
                if item.status != "ready"
            ],
        }
        output_path = project_dir / "runs" / run_id / "artifacts" / "result_manifest.json"
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        size_bytes = output_path.stat().st_size
        return ArtifactRecord(
            artifact_id=f"artifact_{run_id}_result_manifest",
            kind="result_manifest",
            relative_path=output_path.relative_to(project_dir).as_posix(),
            media_type="application/json",
            created_at=_utc_now(),
            run_id=run_id,
            size_bytes=size_bytes,
            sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
        )

    def _mark_failure(
        self,
        project_id: str,
        run_id: str,
        status: RunStatus,
        code: str,
        message: str,
        *,
        listener_key: str | None = None,
    ) -> None:
        try:
            updated = self._replace_run(
                project_id,
                run_id,
                lambda run: run if run.status not in {
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.REPORTING,
                } else replace(
                    run,
                    status=status,
                    updated_at=_utc_now(),
                    heartbeat_at=_utc_now(),
                    stage="cancelled" if status is RunStatus.CANCELLED else "failed",
                    error={"code": code, "message": message},
                ),
            )
            self._notify(listener_key or run_id, updated)
        except DFMError:
            return

    def _advance_stage(
        self,
        project_id: str,
        run_id: str,
        stage: str,
        percent: int,
        *,
        listener_key: str | None = None,
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
        self._notify(listener_key or run_id, updated)

    def _complete_success(
        self,
        project_id: str,
        run_id: str,
        artifacts: list[ArtifactRecord],
    ) -> None:
        now = _utc_now()
        requires_agent_report = any(
            item.kind == "report_html_runtime" for item in artifacts
        )
        target_status = (
            RunStatus.REPORTING if requires_agent_report else RunStatus.SUCCEEDED
        )

        def complete(current: ProjectManifest) -> ProjectManifest:
            runs = []
            found = False
            for run in current.runs:
                if run.run_id != run_id:
                    runs.append(run)
                    continue
                found = True
                ensure_run_transition(run.status, target_status)
                combined = {item.relative_path: item for item in run.artifacts}
                combined.update({item.relative_path: item for item in artifacts})
                runs.append(
                    replace(
                        run,
                        status=target_status,
                        updated_at=now,
                        heartbeat_at=now,
                        stage=(
                            STAGE_REPORT_EDITING
                            if requires_agent_report
                            else STAGE_COMPLETE
                        ),
                        progress_percent=98 if requires_agent_report else 100,
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
            processed_faces = run.processed_faces
            total_faces = run.total_faces
            elapsed_seconds = run.elapsed_seconds
            if event.type == "progress":
                percent = max(percent, int(event.percent or 0))
                stage = event.stage or stage
                processed_faces = event.processed_faces
                total_faces = event.total_faces
                elapsed_seconds = event.elapsed_seconds
            return replace(
                run,
                artifacts=artifacts,
                progress_percent=percent,
                stage=stage,
                processed_faces=processed_faces,
                total_faces=total_faces,
                elapsed_seconds=elapsed_seconds,
                heartbeat_at=now,
                updated_at=now,
                external_job_id=event.external_job_id or run.external_job_id,
            )

        updated = self._replace_run(project_id, run_id, update)
        if artifact is not None and artifact.kind == "geometric_results":
            run = self._find_run(self._store(project_id).load(), run_id)
            if run.plan_snapshot:
                plan = PlanRecord.from_dict(run.plan_snapshot)
                incremental = self._validate_artifact(project_dir, artifact)
                evaluation = self._validate_artifact(
                    project_dir,
                    self.evaluation_engine.materialize(
                        project_dir,
                        run_id,
                        plan,
                        [incremental],
                    ),
                )
                findings = materialize_evaluated_findings(
                    project_dir, [incremental, evaluation]
                )
                finding_ids = {item.finding_id for item in findings}

                def publish(current: ProjectManifest) -> ProjectManifest:
                    runs = [
                        replace(
                            item,
                            artifacts=[
                                *[
                                    value
                                    for value in item.artifacts
                                    if value.relative_path
                                    != evaluation.relative_path
                                ],
                                evaluation,
                            ],
                        )
                        if item.run_id == run_id
                        else item
                        for item in current.runs
                    ]
                    return replace(
                        current,
                        runs=runs,
                        findings=[
                            item
                            for item in current.findings
                            if item.finding_id not in finding_ids
                        ]
                        + findings,
                        updated_at=now,
                    )

                self._store(project_id).update(publish)
                updated = self.status(project_id, run_id)
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
        if run.status not in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.REPORTING,
        }:
            return run
        with self._lock:
            if run.status is RunStatus.REPORTING:
                job_key = self._report_job_key(run_id)
                token = self._tokens.get(job_key)
                if token:
                    token.cancel()
                future = self._futures.get(job_key)
                cancelled_before_start = bool(future and future.cancel())
                cancelled = self._replace_run(
                    project_id,
                    run_id,
                    lambda item: replace(
                        item,
                        status=RunStatus.CANCELLED,
                        updated_at=_utc_now(),
                        heartbeat_at=_utc_now(),
                        stage="cancelled",
                    ),
                )
                self._notify(job_key, cancelled)
                if cancelled_before_start:
                    self._tokens.pop(job_key, None)
                    self._listeners.pop(job_key, None)
                return cancelled
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

    def attach_artifacts(
        self,
        project_id: str,
        run_id: str,
        artifacts: list[ArtifactRecord],
    ) -> RunRecord:
        """Attach final HTML artifacts and complete a report-pending run."""

        project_dir = self.workspace.project_dir(project_id)
        validated = [self._validate_artifact(project_dir, item) for item in artifacts]
        if any(item.run_id != run_id for item in validated):
            raise DFMError(
                "artifact_invalid",
                "A report artifact belongs to a different DFM run.",
                {"run_id": run_id},
            )
        now = _utc_now()

        def attach(current: ProjectManifest) -> ProjectManifest:
            runs: list[RunRecord] = []
            found = False
            for run in current.runs:
                if run.run_id != run_id:
                    runs.append(run)
                    continue
                found = True
                if run.status not in {RunStatus.REPORTING, RunStatus.SUCCEEDED}:
                    raise DFMError(
                        "result_not_ready",
                        "HTML reports require a report-ready DFM run.",
                        {"run_id": run_id, "status": run.status.value},
                    )
                if run.status is RunStatus.REPORTING and not any(
                    item.kind == "report_html" for item in validated
                ):
                    raise DFMError(
                        "report_artifact_missing",
                        "A report-pending DFM run requires a validated HTML artifact.",
                        {"run_id": run_id},
                    )
                combined = {item.relative_path: item for item in run.artifacts}
                combined.update({item.relative_path: item for item in validated})
                if run.status is RunStatus.REPORTING:
                    ensure_run_transition(run.status, RunStatus.SUCCEEDED)
                runs.append(
                    replace(
                        run,
                        artifacts=list(combined.values()),
                        status=RunStatus.SUCCEEDED,
                        updated_at=now,
                        heartbeat_at=now,
                        stage=STAGE_COMPLETE,
                        progress_percent=100,
                    )
                )
            if not found:
                raise DFMError(
                    "run_not_found", "DFM run was not found.", {"run_id": run_id}
                )
            project_artifacts = {
                item.relative_path: item for item in current.artifacts
            }
            project_artifacts.update(
                {item.relative_path: item for item in validated}
            )
            return replace(
                current,
                runs=runs,
                artifacts=list(project_artifacts.values()),
                updated_at=now,
            )

        updated = self._store(project_id).update(attach)
        return self._find_run(updated, run_id)

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
