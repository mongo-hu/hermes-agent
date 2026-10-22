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
from tools.dfm.runtime.process import (
    GeometrySession,
    GeometrySessionPool,
    ProjectGeometrySession,
    SESSION_CONTRACT,
    geometry_capability_identity,
)


CAPABILITIES = {
    "contract_version": SESSION_CONTRACT,
    "schema_bundle_version": "2.0.0",
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
            "requires_complete_inventory": False,
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

    pool.release_project("dfm-1", "chat-one")
    pool._evict_idle()
    assert session.calls == [
        ("snapshots.close", {"snapshot_id": "snapshot-one"})
    ]
    assert session.closed
    assert "dfm-1" not in pool._projects


def _session_with_recorded_job_calls(statuses):
    session = object.__new__(GeometrySession)
    session._job_event_lock = threading.Lock()
    session._job_event_callbacks = {}
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
    schema_dir = Path(os.environ.get("DFM_GEOMETRY_SCHEMA_ROOT", "dfm-geometry/schemas"))
    executable = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE")
    if not schema_dir.is_dir() or not executable:
        pytest.skip("external DFMAnalysis_OCCT checkout is not available")
    schema = json.loads(
        (schema_dir / "capabilities.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(
        occt_module.probe_geometry_executable(executable)
    )
