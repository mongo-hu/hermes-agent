"""Actual compiled Worker, stdio transport and public evidence consumer."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import pytest
from jsonschema import Draft202012Validator

from tools.dfm.analyzers.occt import _validate_scalar_field
from tools.dfm.runtime.process import GeometrySession, GeometrySessionPool
from tools.dfm.contracts import ArtifactRecord, InputRecord, PlanRecord
from tools.dfm.evidence.field_engine import FieldEvidenceEngine
from tools.dfm.errors import DFMError
from tools.dfm.feature_recognition.occt_cpp import OCCTCppFeatureRecognitionProvider
from tools.dfm.processes.occt_injection import geometry_binding_index
from tools.dfm.config import DFMConfig
from tools.dfm.service import DFMService


@pytest.mark.skipif(
    not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"),
    reason="native Worker is not configured",
)
def test_native_stdio_keeps_the_model_and_returns_consumable_g_evidence(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures/dfm/step/injection_plate_with_hole.step"
    )
    input_path = tmp_path / "model.step"
    shutil.copy2(source, input_path)
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    session = GeometrySession(
        os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"], request_timeout_seconds=30
    )
    try:
        assert "G_WALL_THK_LOCAL" in geometry_binding_index(session.capability)
        opened = session.call(
            "snapshots.open",
            {
                "input_path": str(input_path),
                "input_sha256": digest,
                "artifact_dir": str(tmp_path / "geometry"),
                "lease_seconds": 900,
            },
        )
        submitted = session.call(
            "features.discover",
            {
                "snapshot_id": opened["snapshot_id"],
                "feature_kinds": ["main_wall"],
                "context": {},
            },
        )
        session.call(
            "snapshots.renew",
            {"snapshot_id": opened["snapshot_id"], "lease_seconds": 900},
        )
        discovered = session.wait_job(
            submitted["job_id"], timeout_seconds=120, poll_interval_seconds=0.01
        )
        wall = next(
            item for item in discovered["features"] if item["kind"] == "main_wall"
        )
        assert {
            "annotations",
            "relationships",
            "forming_scopes",
            "coverage",
        } <= discovered.keys()
        request = {
            "snapshot_id": discovered["snapshot_id"],
            "items": [
                {
                    "feature_id": wall["feature_id"],
                    "geometric_ids": ["G_WALL_THK_LOCAL"],
                    "context": {},
                }
            ],
        }

        def compute():
            queued = session.call("geometrics.compute", request)
            return session.wait_job(
                queued["job_id"], timeout_seconds=120, poll_interval_seconds=0.01
            )

        first = compute()
        assert first["results"][0]["status"] == "ok", first
        second = compute()
        assert second["diagnostics"]["model_import_count"] == 1
        assert second["results"][0]["quality"]["calculation_reused"] is True
        assert second["results"][0]["value"] == first["results"][0]["value"]
        assert first["artifacts"]
        for artifact in first["artifacts"]:
            path = Path(artifact["path"])
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
            field = json.loads(path.read_text(encoding="utf-8"))
            _validate_scalar_field(
                field,
                artifact_id=artifact["artifact_id"],
                snapshot_id=discovered["snapshot_id"],
                input_sha256=digest,
                submitted_keys={(wall["feature_id"], "G_WALL_THK_LOCAL", "{}")},
            )
            assert (
                field["scene_ref"]
                == f"scene_{opened['artifacts']['render_mesh_snapshot_id']}"
            )
            linked_artifacts = {}
            for kind, ref in (
                ("render_scene", field["scene_ref"]),
                ("topology_map", field["topology_map_ref"]),
            ):
                linked_artifacts[ref] = ArtifactRecord(
                    ref,
                    kind,
                    Path(opened["artifacts"][kind]).relative_to(tmp_path).as_posix(),
                    "application/json",
                    "now",
                )
            FieldEvidenceEngine._validate_field_identity(
                tmp_path,
                field,
                "run-native",
                digest,
                {**first["results"][0], "snapshot_id": discovered["snapshot_id"]},
                linked_artifacts,
                {},
                {},
            )
    finally:
        session.close()


@pytest.mark.skipif(
    not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"),
    reason="native Worker is not configured",
)
def test_native_first_round_requires_direction_and_merges_structure_with_forming_state(
    tmp_path,
):
    executable = os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"]
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures/dfm/step/injection_plate_with_hole.step"
    )
    project_dir = tmp_path / "project-native"
    project_dir.mkdir()
    path = project_dir / "model.step"
    shutil.copy2(source, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    input_record = InputRecord(
        "input", "step", path.name, path.name, path.stat().st_size, digest, "now"
    )
    pool = GeometrySessionPool(
        executable,
        request_timeout_seconds=30,
        job_timeout_seconds=120,
        poll_interval_seconds=0.01,
        idle_timeout_seconds=300,
        max_sessions=1,
        memory_budget_mb=4096,
    )
    try:
        provider = OCCTCppFeatureRecognitionProvider(executable, session_pool=pool)
        with pytest.raises(DFMError) as error:
            provider.recognize(
                input_record, process="injection", project_dir=project_dir
            )
        assert error.value.code == "direction_required"
        projected = provider.recognize(
            input_record,
            process="injection",
            facts={"pull_dir": [0, 0, 1]},
            project_dir=project_dir,
        )
        assert projected.features
        assert projected.diagnostics["model_import_count"] == 1
        assert any(
            scope["forming_scope_id"] == "MAIN" and scope["kind"] == "main_forming"
            for scope in projected.forming_scopes
        )
        assert {item["feature_kind"] for item in projected.diagnostics["coverage"]} >= {
            "main_wall",
            "forming_scope",
        }
        schema_root = os.environ.get("DFM_GEOMETRY_SCHEMA_ROOT")
        if schema_root:
            schema = json.loads(
                (Path(schema_root) / "session-v2.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            Draft202012Validator(schema).validate(
                pool.acquire(project_dir.name).session.capability
            )
    finally:
        pool.close()


@pytest.mark.skipif(
    not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"),
    reason="native Worker is not configured",
)
def test_native_service_uses_confirmed_direction_without_an_extra_main_review(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures/dfm/step/injection_plate_with_hole.step"
    )
    service = DFMService(
        config=DFMConfig(
            geometry_backend="occt_cpp_external",
            geometry_executable=os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"],
            geometry_timeout_seconds=120,
            geometry_job_poll_interval_ms=20,
        ),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="native incremental review")[
            "project_id"
        ]
        uploaded = service.project("add_input", project_id=project_id, path=str(source))
        assert uploaded["preview"]["status"] == "ready"
        for name, value in {
            "process": "injection",
            "model_units": "mm",
            "pull_dir": [0, 0, 1],
            "material": "ABS",
        }.items():
            service.project(
                "confirm_fact", project_id=project_id, fact_name=name, fact_value=value
            )
        discovered = service.analysis("discover", project_id=project_id)
        wall_id = next(
            item["feature_id"]
            for item in discovered["features"]
            if item["kind"] == "main_wall"
        )
        context = service.analysis("review_context", project_id=project_id)
        wall_group = next(
            group
            for group in context["review_groups"]
            if group["kind"] == "ordinary_structure" and wall_id in group["target_refs"]
        )
        service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=context["revision"],
            review_group_ids=[wall_group["review_group_id"]],
        )
        planned = service.analysis("plan", project_id=project_id)
        assert "plan" in planned, planned
        plan = planned["plan"]
        assert any(item["status"] == "ready" for item in plan["plan_fragments"])
        assert not any(
            item["status"] == "pending_review" for item in plan["plan_fragments"]
        )
        assert all(scope["status"] == "confirmed" for scope in context["forming_scopes"] if scope["mode"] == "main")
        run_id = service.analysis(
            "start", project_id=project_id, plan_id=plan["plan_id"]
        )["run"]["run_id"]

        def wait_for(stages):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                run = service.jobs.status(project_id, run_id)
                if run.stage in stages or run.status.value in {
                    "failed",
                    "blocked",
                    "cancelled",
                }:
                    return run
                time.sleep(0.02)
            pytest.fail(
                f"native run did not reach {stages}: {service.jobs.status(project_id, run_id)}"
            )

        finished = wait_for({"report_editing", "complete"})
        assert finished.stage in {"report_editing", "complete"}, finished
        assert finished.completed_fragment_refs
        assert {"geometric_results", "evaluations", "dfm_viewer"} <= {
            item.kind for item in finished.artifacts
        }
        assert "report_html" not in {item.kind for item in finished.artifacts}
        viewer = next(item for item in finished.artifacts if item.kind == "dfm_viewer")
        view = json.loads(
            (service.workspace.project_dir(project_id) / viewer.relative_path).read_text(encoding="utf-8")
        )
        # Unsupported catalog Gs may still make coverage partial. They must not
        # be confused with an extra pending MAIN confirmation.
        assert view["status"] == "completed"
        assert not view["analysis_coverage"]["pending_inputs"]
        assert not view["analysis_coverage"]["incomplete_fragments"]
        frozen = service._latest_discovery_snapshot(service._store(project_id).load())
        live = service.geometry_sessions.call(
            project_id,
            "snapshots.renew",
            {
                "snapshot_id": service.geometry_sessions.resolve_snapshot_id(
                    project_id, frozen.worker_snapshot_id
                ),
                "lease_seconds": 900,
            },
        )
        assert live["snapshot_id"]
        digest = service._store(project_id).load().inputs[0].sha256
        cached = service.geometry_sessions.acquire(
            project_id
        ).snapshots_by_input_sha256[digest]
        assert cached["diagnostics"]["model_import_count"] == 1
        assert any(
            scope["forming_scope_id"] == "MAIN" and scope["status"] == "confirmed"
            for scope in cached["forming_scopes"]
        )
    finally:
        service.close()


@pytest.mark.skipif(
    not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"),
    reason="native Worker is not configured",
)
def test_native_missing_rule_factor_keeps_a_run_waiting_then_resumes_on_user_input(
    tmp_path,
):
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures/dfm/step/injection_plate_with_hole.step"
    )
    service = DFMService(
        config=DFMConfig(
            geometry_backend="occt_cpp_external",
            geometry_executable=os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"],
            geometry_timeout_seconds=120,
            geometry_job_poll_interval_ms=20,
        ),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="native deferred material")[
            "project_id"
        ]
        service.project("add_input", project_id=project_id, path=str(source))
        for name, value in {
            "process": "injection",
            "model_units": "mm",
            "pull_dir": [0, 0, 1],
        }.items():
            service.project(
                "confirm_fact", project_id=project_id, fact_name=name, fact_value=value
            )
        service.analysis("discover", project_id=project_id)
        context = service.analysis("review_context", project_id=project_id)
        selected = [
            group["review_group_id"]
            for group in context["review_groups"]
            if group["status"] == "pending_review"
            and group["kind"] in {"ordinary_structure", "product_forming"}
        ]
        service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=context["revision"],
            review_group_ids=selected,
        )
        planned = service.analysis("plan", project_id=project_id)
        assert "plan" in planned, planned
        plan = planned["plan"]
        assert any(item["fact_name"] == "material" for item in plan["pending_inputs"])
        assert any(item["status"] == "ready" for item in plan["plan_fragments"])
        run_id = service.analysis(
            "start", project_id=project_id, plan_id=plan["plan_id"]
        )["run"]["run_id"]

        def wait_stage(stages):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                run = service.jobs.status(project_id, run_id)
                if run.stage in stages or run.status.value in {
                    "failed",
                    "blocked",
                    "cancelled",
                }:
                    return run
                time.sleep(0.02)
            pytest.fail(
                f"Run remained {run.status.value}/{run.stage}, expected {stages}"
            )

        parked = wait_stage({"waiting_input"})
        assert parked.stage == "waiting_input", parked
        assert parked.completed_fragment_refs
        assert not any(item.kind == "report_html_runtime" for item in parked.artifacts)
        supplied = service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="material",
            fact_value="ABS",
        )
        assert any(
            item["run_id"] == run_id and item["status"] in {"queued", "running"}
            for item in supplied.get("workflow", [])
        ), supplied
        finished = wait_stage({"report_editing", "complete"})
        assert finished.stage in {"report_editing", "complete"}, finished
        assert set(parked.completed_fragment_refs) <= set(
            finished.completed_fragment_refs
        )
        assert finished.plan_snapshot["pending_inputs"] == []
        artifact = next(
            item for item in finished.artifacts if item.kind == "geometric_results"
        )
        payload = json.loads(
            (
                service.workspace.project_dir(project_id) / artifact.relative_path
            ).read_text(encoding="utf-8")
        )
        active_refs = {
            item.fragment_id
            for item in PlanRecord.from_dict(
                finished.plan_snapshot
            ).active_plan_fragments()
        }
        selected_results = [
            row
            for row in payload["results"]
            if row["provenance"]["plan_fragment_ref"] in active_refs
        ]
        keys = [
            (
                row["feature_id"],
                row["geometric_id"],
                json.dumps(row["context"], sort_keys=True),
            )
            for row in selected_results
        ]
        assert len(keys) == len(set(keys))
        # Superseded bindings retain their history, but valid geometry must be
        # reused when new rule facts merely bind the same G to a new fragment.
        for selected in selected_results:
            older = [
                row
                for row in payload["results"]
                if row["result_id"] != selected["result_id"]
                and row["status"] == selected["status"] == "ok"
                and row["feature_id"] == selected["feature_id"]
                and row["geometric_id"] == selected["geometric_id"]
                and row["context"] == selected["context"]
            ]
            if older:
                assert selected["quality"]["calculation_reused"] is True
                assert all(row["value"] == selected["value"] for row in older)
    finally:
        service.close()
