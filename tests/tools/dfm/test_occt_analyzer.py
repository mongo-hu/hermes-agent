import json
import os
from pathlib import Path
import threading

import pytest
from jsonschema import Draft202012Validator

from tools.dfm.analyzers import occt as occt_module
from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.occt import (
    OcctAnalyzer,
    _validate_scalar_field,
    discover_geometry_executable,
)
from tools.dfm.contracts import (
    CapabilityStatus,
    GeometricRequest,
    PlanFragmentRecord,
    PlanRecord,
)
from tools.dfm.errors import DFMError
from tools.dfm.runtime import process as process_module
from tools.dfm.runtime.process import (
    GeometrySession,
    GeometrySessionPool,
    ProjectGeometrySession,
    SnapshotRecoveryState,
    SESSION_CONTRACT,
    SESSION_SCHEMA_BUNDLE_VERSION,
    geometry_capability_identity,
)


CAPABILITIES = {
    "contract_version": SESSION_CONTRACT,
    "schema_bundle_version": SESSION_SCHEMA_BUNDLE_VERSION,
    "engine_version": "occt-dfm-geometry-test",
    "dependency_fingerprint": "a" * 64,
    "backend": "occt-analysis-situs",
    "occt_version": "7.9.0",
    "analysis_situs_version": "test",
    "analysis_situs_commit": "test",
    "feature_kinds": ["main_wall"],
    "geometrics": [
        {
            "geometric_id": "G_WALL_THK_MIN",
            "support_status": "supported",
            "feature_kind": "main_wall",
            "scope_modes": ["feature"],
            "direction_modes": ["none"],
            "requires_complete_inventory": [],
            "requires_relationships": [],
            "context_schema": {
                "type": "object",
                "additionalProperties": False,
            },
        }
    ],
}


def _plan():
    fragment = PlanFragmentRecord(
        "fragment-test",
        "snapshot-test",
        "discovery-test",
        "2.0.0:test",
        "b" * 64,
        [],
        [],
        [GeometricRequest("feature-test", ["G_WALL_THK_MIN"], {})],
    )
    return PlanRecord(
        "plan-test",
        "step",
        ["occt_cpp"],
        "ready",
        "2026-10-21T00:00:00Z",
        process="injection",
        plan_fragments=[fragment],
    )


def test_capability_probe_accepts_only_the_persistent_v2_contract(tmp_path):
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        capability_probe=lambda executable: CAPABILITIES,
    )
    capability = analyzer.capability(AnalyzerContext("dfm-1", tmp_path, "step", []))

    assert capability.status is CapabilityStatus.AVAILABLE
    assert capability.details["contract_version"] == SESSION_CONTRACT
    assert capability.details["geometric_ids"] == ["G_WALL_THK_MIN"]
    assert capability.details["backend"] == "occt-analysis-situs"


def test_capability_probe_rejects_the_removed_short_lived_contract(tmp_path):
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        capability_probe=lambda executable: {
            "contract_version": "dfm.geometry-capabilities/v1",
            "operations": [],
        },
    )

    capability = analyzer.capability(AnalyzerContext("dfm-1", tmp_path, "step", []))

    assert capability.status is CapabilityStatus.UNHEALTHY
    assert capability.error_code == "geometry_protocol_invalid"


def test_geometry_executable_discovery_prefers_explicit_config_then_path(
    monkeypatch, tmp_path
):
    configured = tmp_path / "configured" / "dfm-geometry.exe"
    configured.parent.mkdir()
    configured.write_bytes(b"")
    path_executable = tmp_path / "path" / "dfm-geometry.exe"
    path_executable.parent.mkdir()
    path_executable.write_bytes(b"")
    monkeypatch.setattr(
        "tools.dfm.analyzers.occt.shutil.which",
        lambda name: str(path_executable),
    )

    assert discover_geometry_executable(str(configured)) == str(configured.resolve())
    assert discover_geometry_executable("dfm-geometry-custom") == str(
        path_executable.resolve()
    )


def test_geometry_executable_relative_config_is_anchored_to_repo_root(
    monkeypatch, tmp_path
):
    repo_root = tmp_path / "checkout"
    module_path = repo_root / "tools" / "dfm" / "analyzers" / "occt.py"
    executable = repo_root / "worker" / "dfm-geometry.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    unrelated_cwd = tmp_path / "desktop-cwd"
    unrelated_cwd.mkdir()

    monkeypatch.setattr(occt_module, "__file__", str(module_path))
    monkeypatch.chdir(unrelated_cwd)

    assert discover_geometry_executable("worker/dfm-geometry.exe") == str(
        executable.resolve()
    )


def test_missing_native_engine_is_an_explicit_dependency_error(tmp_path):
    analyzer = OcctAnalyzer(None)
    capability = analyzer.capability(AnalyzerContext("dfm-1", tmp_path, "step", []))

    assert capability.status is CapabilityStatus.DEPENDENCY_MISSING
    assert capability.error_code == "geometry_dependency_missing"


def test_analysis_requires_the_project_owned_persistent_session(tmp_path):
    analyzer = OcctAnalyzer("C:/dfm/dfm-geometry.exe")

    with pytest.raises(DFMError) as exc_info:
        analyzer.run(
            AnalyzerContext("dfm-1", tmp_path, "step", [], plan=_plan()),
            CancellationToken(),
        )

    assert exc_info.value.code == "geometry_session_required"


def test_analysis_rejects_a_plan_pinned_to_another_worker_build(tmp_path):
    class FakePool:
        @staticmethod
        def acquire(_project_id):
            return type(
                "Owned",
                (),
                {"session": type("Session", (), {"capability": CAPABILITIES})()},
            )()

    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        session_pool=FakePool(),
    )

    with pytest.raises(DFMError) as exc_info:
        analyzer.run(
            AnalyzerContext("dfm-1", tmp_path, "step", [], plan=_plan()),
            CancellationToken(),
        )

    assert exc_info.value.code == "geometry_capability_changed"
    assert exc_info.value.details["live_capability_version"] == (
        geometry_capability_identity(CAPABILITIES)
    )


def test_snapshot_update_merges_sibling_branches_instead_of_overwriting():
    class FakeSession:
        alive = True
        pid = 1

    pool = object.__new__(GeometrySessionPool)
    pool._lock = threading.RLock()
    pool._recovery = {}
    owned = ProjectGeometrySession("dfm-1", FakeSession())
    owned.snapshots_by_input_sha256["a" * 64] = {"snapshot_id": "snapshot-left"}
    owned.snapshot_lease_seconds["snapshot-base"] = 900
    pool.acquire = lambda _project_id: owned
    calls = []

    def call(_project_id, method, params):
        calls.append((method, params))
        return {
            "base_snapshot_id": "snapshot-base",
            "snapshot_id": "snapshot-merged",
            "features": [],
            "forming_scopes": [],
            "coverage": [],
        }

    pool.call = call
    result = pool.update_snapshot(
        "dfm-1",
        "a" * 64,
        {"snapshot_id": "snapshot-base", "artifacts": {}},
        {"base_snapshot_id": "snapshot-base", "snapshot_id": "snapshot-right"},
    )

    assert result["snapshot_id"] == "snapshot-merged"
    assert calls == [
        (
            "snapshots.merge",
            {
                "base_snapshot_id": "snapshot-base",
                "snapshot_ids": ["snapshot-left", "snapshot-right"],
            },
        )
    ]
    assert owned.snapshots_by_input_sha256["a" * 64]["snapshot_id"] == (
        "snapshot-merged"
    )


def test_snapshot_merge_conflict_preserves_the_current_branch():
    class FakeSession:
        alive = True
        pid = 1

    pool = object.__new__(GeometrySessionPool)
    pool._lock = threading.RLock()
    owned = ProjectGeometrySession("dfm-1", FakeSession())
    input_sha256 = "a" * 64
    cached = {"snapshot_id": "snapshot-left", "artifacts": {}}
    owned.snapshots_by_input_sha256[input_sha256] = cached
    pool.acquire = lambda _project_id: owned

    def conflict(_project_id, method, params):
        assert method == "snapshots.merge"
        assert params == {
            "base_snapshot_id": "snapshot-base",
            "snapshot_ids": ["snapshot-left", "snapshot-right"],
        }
        raise DFMError("snapshot_conflict", "fixture conflict")

    pool.call = conflict

    with pytest.raises(DFMError) as exc_info:
        pool.update_snapshot(
            "dfm-1",
            input_sha256,
            {"snapshot_id": "snapshot-base", "artifacts": {}},
            {
                "base_snapshot_id": "snapshot-base",
                "snapshot_id": "snapshot-right",
            },
        )

    assert exc_info.value.code == "snapshot_conflict"
    assert owned.snapshots_by_input_sha256[input_sha256] is cached


@pytest.fixture
def recovery_patch(tmp_path):
    build = dict(CAPABILITIES)
    ref = {"kind": "face", "index": 198, "input_sha256": "a" * 64,
           "topology_snapshot_id": "topology-original", "entity_id": "face-198"}
    topology_path = tmp_path / "topology.json"
    topology_path.write_text(json.dumps({"faces": [
        {"geometry_ref": ref, "triangle_refs": []},
    ]}), encoding="utf-8")
    discovered = {"snapshot_id": "live-discovered", "features": [
        {"feature_id": "f", "algorithm_version": "1.13.0", "geometry_refs": [ref]},
    ], "forming_scopes": []}
    revised = {**discovered, "snapshot_id": "live-revised", "forming_scopes": [
        {"forming_scope_id": "SIDE_01", "geometry_refs": [ref], "direction": [1, 0, 0]},
    ]}
    state = SnapshotRecoveryState(
        input_path=str(tmp_path / "model.step"), input_sha256="a" * 64,
        artifact_dir=str(tmp_path), lease_seconds=900,
        opened_snapshot_id="old-opened", current_snapshot_id="old-revised",
        schema_bundle_version=build["schema_bundle_version"],
        engine_version=build["engine_version"], dependency_fingerprint=build["dependency_fingerprint"],
        steps=[
            {"method": "features.discover", "params": {"snapshot_id": "old-opened"},
             "result_snapshot_id": "old-discovered",
             "result_state_sha256": process_module._snapshot_state_sha256(discovered)},
            {"method": "forming_scopes.revise", "params": {"snapshot_id": "old-discovered",
             "scopes": [{"forming_scope_id": "SIDE_01", "geometry_refs": [dict(ref)]}]},
             "result_snapshot_id": "old-revised",
             "result_state_sha256": process_module._snapshot_state_sha256(revised)},
        ],
    )
    calls = []

    class FakeSession:
        capability = build

        def call(self, method, params):
            calls.append((method, params))
            if method == "snapshots.open":
                return {"snapshot_id": "live-opened", "artifacts": {"topology_map": str(topology_path)}}
            if method == "features.discover":
                return {"job_id": "job-discover"}
            assert params["snapshot_id"] == "live-discovered"
            return revised

        def wait_job(self, _job_id, **_kwargs):
            return discovered

    pool = object.__new__(GeometrySessionPool)
    pool.job_timeout_seconds = 30
    pool.poll_interval_seconds = 0.01
    pool._lock = threading.RLock()
    pool._write_recovery(state)
    owned = ProjectGeometrySession("dfm-1", FakeSession())
    return pool, owned, state, calls


def test_same_build_recovers_unmeshed_face_without_rewriting_review_log(recovery_patch):
    pool, owned, state, calls = recovery_patch
    original = state.path.read_bytes()
    result = pool._recover_model(owned, state)
    assert result["snapshot_id"] == "live-revised"
    assert [method for method, _ in calls] == ["snapshots.open", "features.discover", "forming_scopes.revise"]
    assert state.path.read_bytes() == original
    saved = SnapshotRecoveryState.from_dict(json.loads(original))
    assert state.to_dict() == saved.to_dict()
    pool._recover_model(owned, saved)
    assert state.path.read_bytes() == original


@pytest.mark.parametrize("failure", ["unknown_build", "wrong_sha", "missing_face", "wrong_topology", "unknown_head"])
def test_failed_same_build_recovery_preserves_review_log(recovery_patch, failure):
    pool, owned, state, calls = recovery_patch
    if failure == "unknown_build":
        owned.session.capability = {**owned.session.capability, "dependency_fingerprint": "d" * 64}
    elif failure == "wrong_sha":
        state.steps[1]["result_state_sha256"] = "0" * 64
    elif failure in {"missing_face", "wrong_topology"}:
        ref = state.steps[1]["params"]["scopes"][0]["geometry_refs"][0]
        ref["index" if failure == "missing_face" else "topology_snapshot_id"] = 999 if failure == "missing_face" else "other"
    elif failure == "unknown_head":
        state.current_snapshot_id = "not-replayed"
    pool._write_recovery(state)
    original = state.path.read_bytes()
    with pytest.raises(DFMError):
        pool._recover_model(owned, state)
    assert state.path.read_bytes() == original
    assert not owned.snapshot_aliases
    assert not owned.snapshots_by_input_sha256
    if failure in {"missing_face", "wrong_topology"}:
        assert [method for method, _ in calls] == ["snapshots.open"]
    if failure == "unknown_build":
        assert calls == []


def test_failed_recovery_log_replace_keeps_original_and_cleans_its_temporary(recovery_patch, monkeypatch):
    pool, _owned, state, _calls = recovery_patch
    original = state.path.read_bytes()
    other_writer = state.path.with_name(".session-recovery.json.other-writer.tmp")
    other_writer.write_bytes(b"another writer")
    state.engine_version = "new-build"
    def failed_replace(_path, _target):
        raise OSError("fixture replace failure")
    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(OSError):
        pool._write_recovery(state)
    assert state.path.read_bytes() == original
    assert list(state.path.parent.glob(".session-recovery.json.*.tmp")) == [other_writer]
    assert other_writer.read_bytes() == b"another writer"


def test_active_snapshot_is_renewed_without_replacing_its_session():
    class StopAfterOnePass:
        def __init__(self):
            self.calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    class FakeSession:
        alive = True
        pid = 1

        def __init__(self):
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return {"contract_version": SESSION_CONTRACT, "valid": True}

    pool = object.__new__(GeometrySessionPool)
    pool._lock = threading.RLock()
    pool._maintenance_stop = StopAfterOnePass()
    pool.idle_timeout_seconds = 300
    pool.memory_budget_bytes = 2**63
    pool._references = {"dfm-1": {"chat-1"}}
    session = FakeSession()
    owned = ProjectGeometrySession(
        "dfm-1", session, reference_ids={"chat-1"}
    )
    owned.snapshot_ids.add("snapshot-one")
    owned.snapshot_lease_seconds["snapshot-one"] = 3
    owned.snapshot_last_renewed["snapshot-one"] = 0
    pool._projects = {"dfm-1": owned}

    pool._maintain_snapshot_leases()

    assert pool._projects["dfm-1"].session is session
    assert session.calls == [
        (
            "snapshots.renew",
            {"snapshot_id": "snapshot-one", "lease_seconds": 3},
        )
    ]
    assert owned.snapshot_last_renewed["snapshot-one"] > 0


def test_memory_budget_evicts_only_an_unreferenced_project(monkeypatch):
    class FakeSession:
        alive = True

        def __init__(self, pid):
            self.pid = pid
            self.closed = False

        def call(self, _method, _params):
            return {}

        def close(self):
            self.closed = True

    pool = object.__new__(GeometrySessionPool)
    pool._lock = threading.RLock()
    pool.memory_budget_bytes = 150
    pool._references = {"active": {"chat-1"}}
    idle_session = FakeSession(1)
    active_session = FakeSession(2)
    idle = ProjectGeometrySession("idle", idle_session)
    idle.last_used = 1
    active = ProjectGeometrySession(
        "active", active_session, reference_ids={"chat-1"}
    )
    active.last_used = 2
    pool._projects = {"idle": idle, "active": active}
    monkeypatch.setattr(process_module, "_resident_bytes", lambda _pid: 100)

    resident = pool._evict_for_memory()

    assert resident == 100
    assert idle_session.closed is True
    assert active_session.closed is False
    assert set(pool._projects) == {"active"}


def test_idle_reclamation_waits_for_chat_reference_then_closes_snapshot():
    class FakeSession:
        alive = True
        pid = 1

        def __init__(self):
            self.calls = []
            self.closed = False

        def call(self, method, params):
            self.calls.append((method, params))
            return {}

        def close(self):
            self.closed = True

    pool = object.__new__(GeometrySessionPool)
    pool._lock = threading.RLock()
    pool.idle_timeout_seconds = 0
    pool._references = {}
    session = FakeSession()
    owned = ProjectGeometrySession("dfm-1", session)
    owned.snapshot_ids.add("snapshot-one")
    pool._projects = {"dfm-1": owned}

    pool.retain_project("dfm-1", "chat-one")
    pool._evict_idle()
    assert not session.closed

    owned.active_job_ids.add("job-one")
    pool.release_project("dfm-1", "chat-one")
    pool._evict_idle()
    assert not session.closed
    assert session.calls == []

    owned.active_job_ids.remove("job-one")
    pool._evict_idle()
    assert session.calls == [
        ("snapshots.close", {"snapshot_id": "snapshot-one"})
    ]
    assert session.closed
    assert "dfm-1" not in pool._projects


def _session_with_recorded_job_calls(statuses):
    session = object.__new__(GeometrySession)
    session._job_event_lock = threading.Lock()
    session._job_events = {}
    calls = []

    def call(method, params, **_kwargs):
        calls.append((method, dict(params)))
        if method == "jobs.cancel":
            return {"contract_version": SESSION_CONTRACT, "status": "cancelled"}
        return {
            "contract_version": SESSION_CONTRACT,
            "status": statuses.pop(0),
        }

    session.call = call
    return session, calls


def test_persistent_job_cancellation_cancels_only_the_worker_job():
    session, calls = _session_with_recorded_job_calls(["cancelled"])
    cancellation = CancellationToken()
    cancellation.cancel()

    with pytest.raises(DFMError) as exc_info:
        session.wait_job(
            "job-1",
            timeout_seconds=10,
            poll_interval_seconds=0,
            cancellation=cancellation,
        )

    assert exc_info.value.code == "run_cancelled"
    assert calls == [
        ("jobs.cancel", {"job_id": "job-1"}),
        ("jobs.status", {"job_id": "job-1"}),
    ]


def test_persistent_job_timeout_cancels_job_without_closing_shared_session():
    session, calls = _session_with_recorded_job_calls(["running"])

    with pytest.raises(DFMError) as exc_info:
        session.wait_job(
            "job-2",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )

    assert exc_info.value.code == "geometry_operation_timeout"
    assert calls == [
        ("jobs.status", {"job_id": "job-2"}),
        ("jobs.cancel", {"job_id": "job-2"}),
    ]


def test_scalar_field_accepts_only_feature_g_context_identity():
    context = {}
    context_json = json.dumps(context, sort_keys=True, separators=(",", ":"))
    submitted = {("feature-test", "G_WALL_THK_MIN", context_json)}
    payload = {
        "schema_version": 2,
        "contract_version": SESSION_CONTRACT,
        "field_id": "field-test",
        "snapshot_id": "snapshot-test",
        "input_sha256": "c" * 64,
        "feature_id": "feature-test",
        "geometric_ids": ["G_WALL_THK_MIN"],
        "context": context,
        "unit": "mm",
        "scene_ref": "scene-test",
        "topology_map_ref": "topology-test",
        "topology_snapshot_ref": "topology-snapshot-test",
        "render_mesh_snapshot_ref": "render-snapshot-test",
        "interpolation": {},
        "calculation_context": {},
        "feature_refs": ["feature-test"],
        "samples": [],
        "cells": [],
        "quality": {},
    }

    _validate_scalar_field(
        payload,
        artifact_id="field-test",
        snapshot_id="snapshot-test",
        input_sha256="c" * 64,
        submitted_keys=submitted,
    )
    payload["task_id"] = "removed-routing-id"
    with pytest.raises(DFMError) as exc_info:
        _validate_scalar_field(
            payload,
            artifact_id="field-test",
            snapshot_id="snapshot-test",
            input_sha256="c" * 64,
            submitted_keys=submitted,
        )
    assert exc_info.value.code == "geometry_protocol_invalid"


def test_native_capability_schema_validates_real_executable():
    root = Path(__file__).resolve().parents[3]
    configured_schema_root = os.environ.get("DFM_GEOMETRY_SCHEMA_ROOT")
    schema_candidates = [
        *(
            [Path(configured_schema_root).expanduser()]
            if configured_schema_root
            else []
        ),
        root / "dfm-geometry" / "schemas",
        root.parent.parent / "模具项目" / "occt-worker" / "schemas",
        root.parent / "occt-worker" / "schemas",
        root.parent.parent / "occt-worker" / "schemas",
    ]
    schema_dir = next(
        (item.resolve() for item in schema_candidates if item.is_dir()), None
    )
    executable = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE")
    if schema_dir is None or not executable:
        pytest.skip("external DFMAnalysis_OCCT checkout is not available")
    schema = json.loads(
        (schema_dir / "session-v2.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(
        occt_module.probe_geometry_executable(executable)
    )
