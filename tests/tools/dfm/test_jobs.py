import json
import time
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.base import AnalyzerContext
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.config import DFMConfig
from tools.dfm.contracts import (
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    PlanRecord,
    RunRecord,
    RunStatus,
    WorkerEvent,
)
from tools.dfm.errors import DFMError
from tools.dfm.project.manifest import ManifestStore
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.runtime.jobs import JobManager


class ControlledAnalyzer:
    key = "test"
    version = "1"
    supported_inputs = ("step",)

    def __init__(self, *, fail=False):
        self.started = Event()
        self.release = Event()
        self.fail = fail
        self.contexts = []

    def capability(self, context):
        return Capability(self.key, CapabilityStatus.AVAILABLE, "test only")

    def run(self, context: AnalyzerContext, cancellation):
        self.contexts.append(context)
        self.started.set()
        while not self.release.wait(0.01):
            cancellation.raise_if_cancelled()
        cancellation.raise_if_cancelled()
        if self.fail:
            raise RuntimeError("secret internal failure")
        path = context.project_dir / "artifacts" / f"{context.run_id}.json"
        path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return [
            ArtifactRecord(
                f"artifact_{context.run_id}",
                "diagnostic",
                f"artifacts/{context.run_id}.json",
                "application/json",
                "2026-07-14T00:00:00Z",
            )
        ]


class ProgressAnalyzer(ControlledAnalyzer):
    def run(self, context: AnalyzerContext, cancellation):
        self.contexts.append(context)
        output = context.project_dir / "runs" / context.run_id / "artifacts"
        output.mkdir(parents=True, exist_ok=True)
        partial = output / "partial.png"
        partial.write_bytes(b"partial-image")
        assert context.event_sink is not None
        context.event_sink(
            WorkerEvent(
                1,
                "progress",
                stage="measure_wall_thickness_faces",
                percent=42,
                processed_faces=21,
                total_faces=50,
                elapsed_seconds=5.5,
            )
        )
        context.event_sink(
            WorkerEvent(1, "artifact", kind="evidence_image", path="partial.png")
        )
        self.started.set()
        while not self.release.wait(0.01):
            cancellation.raise_if_cancelled()
        return []


class InspectingExecutor:
    def __init__(self, inspect):
        self.inspect = inspect

    def submit(self, fn, *args):
        self.inspect()
        return Future()

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class RejectingExecutor:
    def submit(self, fn, *args):
        raise RuntimeError("executor unavailable")

    def shutdown(self, wait=True, cancel_futures=False):
        return None


@pytest.fixture
def job_env(tmp_path):
    token = set_hermes_home_override(tmp_path / "home")
    workspace = DFMWorkspace()
    project = workspace.create_project("Bracket")
    registry = AnalyzerRegistry()
    analyzer = ControlledAnalyzer()
    registry.register(analyzer)
    managers = []
    try:
        yield workspace, project.project_id, registry, analyzer, managers
    finally:
        for manager in managers:
            manager.shutdown()
        reset_hermes_home_override(token)


def _wait_status(manager, project_id, run_id, expected, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = manager.status(project_id, run_id)
        if run.status is expected:
            return run
        time.sleep(0.01)
    pytest.fail(f"run did not reach {expected}: {manager.status(project_id, run_id).status}")


def test_start_persists_queued_before_executor_submission(job_env):
    workspace, project_id, registry, _, managers = job_env
    observed = []
    executor = InspectingExecutor(
        lambda: observed.append(ManifestStore(workspace.project_dir(project_id)).load().runs[0].status)
    )
    manager = JobManager(workspace, registry, DFMConfig(), executor=executor, reconcile=False)
    managers.append(manager)

    run = manager.start(project_id, "test")

    assert run.status is RunStatus.QUEUED
    assert observed == [RunStatus.QUEUED]


def test_run_succeeds_and_registers_safe_artifact(job_env):
    workspace, project_id, registry, analyzer, managers = job_env
    manager = JobManager(workspace, registry, DFMConfig())
    managers.append(manager)

    run = manager.start(project_id, "test", idempotency_key="same")
    assert analyzer.started.wait(1)
    analyzer.release.set()
    finished = _wait_status(manager, project_id, run.run_id, RunStatus.SUCCEEDED)
    repeated = manager.start(project_id, "test", idempotency_key="same")

    assert repeated.run_id == run.run_id
    assert finished.artifacts[0].relative_path.startswith("artifacts/")
    assert (workspace.project_dir(project_id) / finished.artifacts[0].relative_path).exists()


def test_run_persists_incremental_progress_artifacts_and_event_log(job_env):
    workspace, project_id, _, _, managers = job_env
    analyzer = ProgressAnalyzer()
    registry = AnalyzerRegistry()
    registry.register(analyzer)
    manager = JobManager(workspace, registry, DFMConfig())
    managers.append(manager)

    updates = []
    run = manager.start(project_id, "test", on_update=updates.append)
    assert analyzer.started.wait(1)
    running = manager.status(project_id, run.run_id)

    assert running.status is RunStatus.RUNNING
    assert running.stage == "measure_wall_thickness_faces"
    assert running.progress_percent == 42
    assert running.processed_faces == 21
    assert running.total_faces == 50
    assert running.elapsed_seconds == 5.5
    assert running.heartbeat_at is not None
    assert [item.kind for item in running.artifacts] == ["evidence_image"]
    assert running.event_log_path is not None
    assert updates[-1].progress_percent == 42
    event_log = workspace.project_dir(project_id) / running.event_log_path
    assert '"measure_wall_thickness_faces"' in event_log.read_text(encoding="utf-8")

    analyzer.release.set()
    finished = _wait_status(manager, project_id, run.run_id, RunStatus.SUCCEEDED)
    assert finished.progress_percent == 100
    assert finished.artifacts == running.artifacts
    deadline = time.monotonic() + 1
    while updates[-1].status is not RunStatus.SUCCEEDED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert updates[-1].status is RunStatus.SUCCEEDED


def test_run_executes_and_persists_the_named_plan_snapshot(job_env):
    workspace, project_id, registry, analyzer, managers = job_env
    manager = JobManager(workspace, registry, DFMConfig())
    managers.append(manager)
    plan = PlanRecord(
        "plan_m1",
        "step",
        ["test"],
        "ready",
        "2026-07-15T00:00:00Z",
        process="injection",
        scope_id="injection.wall-draft",
        scope_version="1.0.0",
    )

    run = manager.start(project_id, "test", plan=plan)
    assert analyzer.started.wait(1)
    analyzer.release.set()
    finished = _wait_status(manager, project_id, run.run_id, RunStatus.SUCCEEDED)

    assert finished.plan_id == plan.plan_id
    assert finished.plan_snapshot == plan.to_dict()
    assert analyzer.contexts[0].plan == plan


def test_run_can_be_cancelled_cooperatively(job_env):
    workspace, project_id, registry, analyzer, managers = job_env
    manager = JobManager(workspace, registry, DFMConfig())
    managers.append(manager)
    run = manager.start(project_id, "test")
    assert analyzer.started.wait(1)

    manager.cancel(project_id, run.run_id)

    assert _wait_status(manager, project_id, run.run_id, RunStatus.CANCELLED)


def test_analyzer_exception_becomes_sanitized_failed_run(job_env):
    workspace, project_id, registry, _, managers = job_env
    failing = ControlledAnalyzer(fail=True)
    registry = AnalyzerRegistry()
    registry.register(failing)
    manager = JobManager(workspace, registry, DFMConfig())
    managers.append(manager)
    run = manager.start(project_id, "test")
    assert failing.started.wait(1)
    failing.release.set()

    failed = _wait_status(manager, project_id, run.run_id, RunStatus.FAILED)

    assert failed.error["code"] == "analyzer_failed"
    assert "secret internal failure" not in failed.error["message"]


def test_reconcile_blocks_stale_incomplete_runs(job_env):
    workspace, project_id, registry, _, managers = job_env
    store = ManifestStore(workspace.project_dir(project_id))
    store.update(
        lambda current: replace(
            current,
            runs=[
                RunRecord(
                    "run_stale",
                    "test",
                    "1",
                    RunStatus.RUNNING,
                    "2026-07-14T00:00:00Z",
                    "2026-07-14T00:00:00Z",
                )
            ],
        )
    )

    manager = JobManager(workspace, registry, DFMConfig(), reconcile=True)
    managers.append(manager)

    recovered = manager.status(project_id, "run_stale")
    assert recovered.status is RunStatus.BLOCKED
    assert recovered.error["code"] == "runtime_restarted"


def test_second_manager_does_not_block_run_owned_by_live_process(job_env):
    workspace, project_id, registry, analyzer, managers = job_env
    first = JobManager(workspace, registry, DFMConfig())
    managers.append(first)
    run = first.start(project_id, "test")
    assert analyzer.started.wait(1)

    second = JobManager(workspace, registry, DFMConfig(), reconcile=True)
    managers.append(second)

    observed = second.status(project_id, run.run_id).status
    analyzer.release.set()
    assert observed is RunStatus.RUNNING
    _wait_status(first, project_id, run.run_id, RunStatus.SUCCEEDED)


def test_executor_submit_failure_does_not_leave_queued_run(job_env):
    workspace, project_id, registry, _, managers = job_env
    manager = JobManager(
        workspace,
        registry,
        DFMConfig(),
        executor=RejectingExecutor(),
        reconcile=False,
    )
    managers.append(manager)

    run = manager.start(project_id, "test")
    persisted = manager.status(project_id, run.run_id)

    assert persisted.status is RunStatus.FAILED
    assert persisted.error["code"] == "runtime_submit_failed"
