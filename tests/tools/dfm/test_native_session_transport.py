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

@pytest.mark.skipif(not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"), reason="native Worker not configured")
def test_more_than_256_native_g_results_and_check_evaluations_survive_all_batches(tmp_path):
    import math
    from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
    from tools.dfm.analyzers.occt import OcctAnalyzer
    from tools.dfm.contracts import EffectiveRule, GeometricRequest, GeometricRuleBinding, PlanFragmentRecord
    from tools.dfm.evaluation.engine import EvaluationEngine
    from tools.dfm.runtime.process import geometry_capability_identity
    from tools.dfm.runtime.result_documents import clear_result_document_cache, read_result_document

    project_dir = tmp_path / "native-capacity"
    project_dir.mkdir()
    source = Path(__file__).resolve().parents[2] / "fixtures/dfm/step/injection_plate_with_hole.step"
    model = project_dir / "model.step"
    shutil.copy2(source, model)
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    input_record = InputRecord(
        input_id="capacity-input", kind="step", source_name="model.step", relative_path="model.step",
        size_bytes=model.stat().st_size, sha256=digest, created_at="2026-11-03T00:00:00Z")
    executable = os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"]
    pool = GeometrySessionPool(executable, request_timeout_seconds=120, job_timeout_seconds=120,
                               poll_interval_seconds=0.01, idle_timeout_seconds=300, max_sessions=1, memory_budget_mb=4096)
    try:
        discovery = OCCTCppFeatureRecognitionProvider(executable, session_pool=pool).recognize(
            input_record, process="injection", facts={"pull_dir": [0, 0, 1]}, project_dir=project_dir)
        wall = next(row for row in discovery.features if row.kind == "main_wall")
        refs = wall.properties["geometry_refs"]
        scopes, contexts = [], []
        for index in range(300):
            angle = index * 0.01
            scale = math.sqrt(0.5)
            context = {"forming_scope_id": f"CAPACITY_{index}",
                       "draw_direction": [scale * math.cos(angle), scale * math.sin(angle), scale],
                       "coordinate_system": "model", "direction_source": "user_confirmed"}
            scopes.append({**context, "kind": "local_forming", "mode": "local", "mechanism_kind": "slider",
                           "geometry_refs": refs, "status": "confirmed"})
            contexts.append(context)
        scoped = pool.call(project_dir.name, "forming_scopes.revise",
                           {"snapshot_id": discovery.worker_snapshot_id, "scopes": scopes})
        capability = pool.acquire(project_dir.name).session.capability
        identity = geometry_capability_identity(capability)
        g = "G_WALL_IN_DRAFT"
        unit = next(row["canonical_unit"] for row in capability["geometrics"] if row["geometric_id"] == g)
        bindings, fragments, rules = [], [], {}
        for index, context in enumerate(contexts):
            binding = GeometricRuleBinding(
                binding_id=f"capacity-binding-{index}", geometric_id=g, rule_id=f"capacity-rule-{index}",
                operator=">=", aggregation="identity", feature_refs=[wall.feature_id], context=context,
                check_id=f"C_CAPACITY_FIXTURE_{index}", operand_alias="actual")
            bindings.append(binding)
            rules[binding.rule_id] = EffectiveRule(1.0, unit, "fixture:not_production")
            fragments.append(PlanFragmentRecord(
                fragment_id=f"capacity-fragment-{index}", snapshot_id=scoped["snapshot_id"],
                source_discovery_snapshot_id="capacity-discovery-fixture", capability_version=identity,
                dependency_fingerprint=hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest(),
                confirmed_fact_refs=[], rule_binding_refs=[binding.binding_id],
                items=[GeometricRequest(wall.feature_id, [g], context)]))
        plan = PlanRecord(
            plan_id="capacity-plan", input_mode="step", analyzer_keys=["occt_cpp"], status="ready",
            created_at="2026-11-03T00:00:00Z", process="injection",
            rules=rules, rule_bindings=bindings, plan_fragments=fragments)
        run_id = "capacity-run"
        artifacts = OcctAnalyzer(executable, session_pool=pool).run(
            AnalyzerContext(project_dir.name, project_dir, "step", [input_record], run_id, plan),
            CancellationToken())
        result_artifact = next(row for row in artifacts if row.kind == "geometric_results")
        path = project_dir / result_artifact.relative_path
        clear_result_document_cache()
        results = read_result_document(path, "geometric_results")["results"]
        assert len(results) == len(contexts)
        assert all(row["status"] == "ok" for row in results)
        assert {row["context"]["forming_scope_id"] for row in results} == {row["forming_scope_id"] for row in scopes}
        assert len({row["result_id"] for row in results}) == len(contexts)
        assert len([row for row in artifacts if row.kind == "geometric_batch"]) > 1
        evaluated = EvaluationEngine().materialize(project_dir, run_id, plan, [result_artifact])
        clear_result_document_cache()
        evaluations = read_result_document(project_dir / evaluated.relative_path, "evaluations")["evaluations"]
        assert len(evaluations) == len(contexts)
        assert {row["rule_id"] for row in evaluations} == set(rules)
        assert all(row["outcome"] in {"pass", "fail"} for row in evaluations)
        after = pool.call(project_dir.name, "snapshots.get", {"snapshot_id": scoped["snapshot_id"]})
        for key in ("features", "annotations", "forming_scopes", "relationships", "coverage"):
            assert after[key] == scoped[key]
        assert after["diagnostics"]["model_import_count"] == 1
        for artifact in artifacts:
            if artifact.kind == "scalar_field":
                assert (project_dir / artifact.relative_path).is_file()
    finally:
        pool.close()

@pytest.mark.skipif(not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"), reason="native Worker not configured")
def test_native_job_capacity_keeps_controls_live_and_releases_terminal_job_models(tmp_path):
    import math
    from concurrent.futures import ThreadPoolExecutor

    session = GeometrySession(os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"], request_timeout_seconds=120)
    try:
        source = Path(__file__).resolve().parents[2] / "fixtures/dfm/step/injection_plate_with_hole.step"
        models = []
        for index in range(2):
            path = tmp_path / f"model-{index}.step"
            path.write_bytes(source.read_bytes() + b"\n" * index)
            params = {"input_path": str(path), "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "artifact_dir": str(tmp_path / f"geometry-{index}"), "lease_seconds": 900}
            models.append((params, session.call("snapshots.open", params)))
        opened = models[0][1]
        submitted = session.call("features.discover",
            {"snapshot_id": opened["snapshot_id"], "feature_kinds": ["main_wall"], "context": {}})
        discovery = session.wait_job(submitted["job_id"], timeout_seconds=120, poll_interval_seconds=0.01)
        feature = next(row for row in discovery["features"] if row["kind"] == "main_wall")
        scopes, items = [], []
        for index in range(300):
            angle, scale = index * 0.01, math.sqrt(0.5)
            context = {"forming_scope_id": f"QUEUE_{index}",
                       "draw_direction": [scale * math.cos(angle), scale * math.sin(angle), scale],
                       "coordinate_system": "model", "direction_source": "user_confirmed"}
            scopes.append({**context, "kind": "local_forming", "mode": "local", "mechanism_kind": "slider",
                           "geometry_refs": feature["geometry_refs"], "status": "confirmed"})
            items.append({"feature_id": feature["feature_id"], "geometric_ids": ["G_WALL_IN_DRAFT"], "context": context})
        scoped = session.call("forming_scopes.revise", {"snapshot_id": discovery["snapshot_id"], "scopes": scopes})
        held = session.call("geometrics.compute", {"snapshot_id": scoped["snapshot_id"], "items": items})
        held_params = {"job_id": held["job_id"]}
        deadline = time.monotonic() + 10
        while session.call("jobs.status", held_params)["status"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert session.call("jobs.status", held_params)["status"] == "running"
        other = {"snapshot_id": models[1][1]["snapshot_id"], "feature_kinds": ["main_wall"], "context": {}}
        first_queued = session.call("features.discover", other)["job_id"]
        assert session.call("jobs.status", {"job_id": first_queued})["status"] == "queued"
        assert session.call("jobs.status", held_params)["status"] == "running"

        def submit(_):
            try:
                return session.call("features.discover", other)
            except DFMError as error:
                assert error.code == "worker_busy"
                return {"busy": True}

        with ThreadPoolExecutor(max_workers=8) as executor:
            replies = list(executor.map(submit, range(300)))
        assert any(reply.get("busy") for reply in replies)
        queued = [first_queued, *[reply["job_id"] for reply in replies if "job_id" in reply]]
        assert len(queued) <= 255
        assert session.call("capabilities.get", {})["contract_version"] == "dfm.geometry/v2"
        session.call("snapshots.renew", {"snapshot_id": scoped["snapshot_id"], "lease_seconds": 900})
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda identity: session.call("jobs.cancel", {"job_id": identity}), queued))
        session.call("jobs.cancel", held_params)
        terminal = {"completed", "failed", "cancelled"}
        for identity in [held["job_id"], *queued]:
            deadline = time.monotonic() + 30
            status = session.call("jobs.status", {"job_id": identity})
            while status["status"] not in terminal and time.monotonic() < deadline:
                time.sleep(0.01)
                status = session.call("jobs.status", {"job_id": identity})
            assert status["status"] in terminal
            result = status.get("result")
            if isinstance(result, dict) and result.get("snapshot_id") not in {
                opened["snapshot_id"], discovery["snapshot_id"], scoped["snapshot_id"], models[1][1]["snapshot_id"]}:
                session.call("snapshots.close", {"snapshot_id": result["snapshot_id"]})
        for identity in (opened["snapshot_id"], discovery["snapshot_id"], scoped["snapshot_id"], models[1][1]["snapshot_id"]):
            session.call("snapshots.close", {"snapshot_id": identity})
        # Terminal Job history keeps JSON evidence, never a live prepared-model closure.
        reopened = session.call("snapshots.open", models[0][0])
        assert reopened["diagnostics"]["model_import_count"] == 3
    finally:
        session.close()
