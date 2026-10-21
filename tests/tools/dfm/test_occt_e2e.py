from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.occt import OcctAnalyzer
from tools.dfm.contracts import (
    ArtifactRecord,
    EffectiveRule,
    GeometricRequest,
    GeometricResultRecord,
    GeometricRuleBinding,
    InputRecord,
    PlanFragmentRecord,
    PlanRecord,
)
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.config import DFMConfig
from tools.dfm.feature_recognition.occt_cpp import OCCTCppFeatureRecognitionProvider
from tools.dfm.runtime.process import GeometrySessionPool
from tools.dfm.runtime.jobs import JobManager
from tools.dfm.service import DFMService


EXECUTABLE = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE", "")


def test_evaluation_consumes_geometric_identity_without_legacy_routing(tmp_path):
    output_dir = tmp_path / "runs" / "run-v2" / "artifacts"
    output_dir.mkdir(parents=True)
    result_path = output_dir / "geometric_results.json"
    result_path.write_text(
        json.dumps({
            "contract_version": "dfm.geometry/v2",
            "run_id": "run-v2",
            "input_sha256": "b" * 64,
            "results": [
                {
                    "result_id": "geometric_result_1",
                    "snapshot_id": "snapshot_v2",
                    "feature_id": "feature-main-wall-1",
                    "geometric_id": "G_WALL_THK_MIN",
                    "status": "ok",
                    "value": 1.2,
                    "unit": "mm",
                    "context": {},
                    "evidence": {"geometry_refs": [], "field_refs": []},
                    "quality": {},
                    "reason": None,
                    "provenance": {
                        "source_snapshot_id": "snapshot_v2",
                        "source_result_ref": "source-1",
                        "dependency_fingerprint": "c" * 64,
                        "plan_fragment_ref": "fragment-v2",
                    },
                    "input_sha256": "b" * 64,
                }
            ],
        }),
        encoding="utf-8",
    )
    request = GeometricRequest("feature-main-wall-1", ["G_WALL_THK_MIN"], {})
    fragment = PlanFragmentRecord(
        fragment_id="fragment-v2",
        snapshot_id="snapshot_v2",
        source_discovery_snapshot_id="discovery.snapshot.v2",
        capability_version="test",
        dependency_fingerprint="d" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=["binding-v2"],
        items=[request],
    )
    binding = GeometricRuleBinding(
        binding_id="binding-v2",
        geometric_id="G_WALL_THK_MIN",
        rule_id="rule-v2",
        operator="<=",
        aggregation="identity",
        feature_refs=["feature-main-wall-1"],
    )
    plan = PlanRecord(
        plan_id="plan-v2",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:00:00Z",
        rules={"rule-v2": EffectiveRule(2.0, "mm", "ontology:test")},
        rule_bindings=[binding],
        plan_fragments=[fragment],
    )
    artifact = ArtifactRecord(
        "artifact-results",
        "geometric_results",
        result_path.relative_to(tmp_path).as_posix(),
        "application/json",
        "2026-01-01T00:00:00Z",
    )
    evaluated = EvaluationEngine().materialize(tmp_path, "run-v2", plan, [artifact])
    payload = json.loads(
        (tmp_path / evaluated.relative_path).read_text(encoding="utf-8")
    )
    row = payload["evaluations"][0]
    assert row["geometric_id"] == "G_WALL_THK_MIN"
    assert row["outcome"] == "pass"
    assert "operation_id" not in row
    assert "metric_id" not in row
    assert "region_refs" not in row


def test_non_ok_geometric_result_is_indeterminate_not_an_error(tmp_path):
    request = GeometricRequest("feature-main-wall-1", ["G_WALL_IN_DRAFT"], {})
    fragment = PlanFragmentRecord(
        fragment_id="fragment-v2-missing-context",
        snapshot_id="snapshot_v2",
        source_discovery_snapshot_id="discovery.snapshot.v2",
        capability_version="test",
        dependency_fingerprint="d" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=["binding-v2-missing-context"],
        items=[request],
    )
    binding = GeometricRuleBinding(
        binding_id="binding-v2-missing-context",
        geometric_id="G_WALL_IN_DRAFT",
        rule_id="rule-v2",
        operator=">=",
        aggregation="identity",
        feature_refs=["feature-main-wall-1"],
    )
    plan = PlanRecord(
        plan_id="plan-v2",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:00:00Z",
        rules={"rule-v2": EffectiveRule(1.0, "deg", "ontology:test")},
        rule_bindings=[binding],
        plan_fragments=[fragment],
    )
    result = GeometricResultRecord(
        result_id="geometric_result_missing_context",
        snapshot_id="snapshot_v2",
        feature_id="feature-main-wall-1",
        geometric_id="G_WALL_IN_DRAFT",
        status="missing_context",
        value=None,
        unit=None,
        context={},
        evidence={"geometry_refs": [], "field_refs": []},
        quality=None,
        reason="a confirmed forming scope is required",
        provenance={
            "source_snapshot_id": "snapshot_v2",
            "source_result_ref": None,
            "dependency_fingerprint": None,
            "plan_fragment_ref": "fragment-v2-missing-context",
        },
        input_sha256="b" * 64,
    )

    evaluations, provenance = EvaluationEngine().evaluate([result], plan)

    assert evaluations[0].outcome == "indeterminate"
    assert evaluations[0].actual is None
    unavailable = evaluations[0].operand_values["actual"]
    assert unavailable["status"] == "unavailable"
    assert unavailable["reason"]["details"]["results"][0]["status"] == (
        "missing_context"
    )
    assert provenance[evaluations[0].evaluation_id]["unavailable_operands"]


def test_evaluation_keeps_identical_g_requests_separate_by_plan_fragment():
    request = GeometricRequest("feature-main-wall-1", ["G_WALL_THK_MIN"], {})
    fragments = [
        PlanFragmentRecord(
            fragment_id=f"fragment-{suffix}",
            snapshot_id=f"snapshot_{suffix}",
            source_discovery_snapshot_id=f"discovery.{suffix}",
            capability_version="2.0.0:test",
            dependency_fingerprint=character * 64,
            confirmed_fact_refs=[],
            rule_binding_refs=[f"binding-{suffix}"],
            items=[request],
        )
        for suffix, character in (("a", "a"), ("b", "b"))
    ]
    bindings = [
        GeometricRuleBinding(
            binding_id=f"binding-{suffix}",
            geometric_id="G_WALL_THK_MIN",
            rule_id=f"rule-{suffix}",
            operator="<=",
            aggregation="identity",
            feature_refs=["feature-main-wall-1"],
        )
        for suffix in ("a", "b")
    ]
    plan = PlanRecord(
        plan_id="plan-two-snapshots",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:00:00Z",
        rules={
            "rule-a": EffectiveRule(2.0, "mm", "ontology:a"),
            "rule-b": EffectiveRule(2.0, "mm", "ontology:b"),
        },
        rule_bindings=bindings,
        plan_fragments=fragments,
    )
    results = [
        GeometricResultRecord(
            result_id=f"result-{suffix}",
            snapshot_id=f"snapshot_{suffix}",
            feature_id="feature-main-wall-1",
            geometric_id="G_WALL_THK_MIN",
            status="ok",
            value=value,
            unit="mm",
            context={},
            evidence={"geometry_refs": [], "field_refs": []},
            quality={},
            reason=None,
            provenance={
                "source_snapshot_id": f"snapshot_{suffix}",
                "source_result_ref": f"source-{suffix}",
                "dependency_fingerprint": character * 64,
                "plan_fragment_ref": f"fragment-{suffix}",
            },
            input_sha256="c" * 64,
        )
        for suffix, character, value in (("a", "a", 1.0), ("b", "b", 3.0))
    ]

    evaluations, _ = EvaluationEngine().evaluate(results, plan)

    assert {item.rule_id: item.outcome for item in evaluations} == {
        "rule-a": "pass",
        "rule-b": "fail",
    }


def test_result_manifest_pins_waiting_fragments_and_non_ok_results(tmp_path):
    output_dir = tmp_path / "runs" / "run-v2" / "artifacts"
    output_dir.mkdir(parents=True)
    result_path = output_dir / "geometric_results.json"
    result_path.write_text(
        json.dumps({
            "contract_version": "dfm.geometry/v2",
            "results": [
                {
                    "result_id": "result-v2",
                    "snapshot_id": "snapshot_v2",
                    "feature_id": "feature-main-wall-1",
                    "geometric_id": "G_WALL_IN_DRAFT",
                    "status": "not_applicable",
                    "reason": "no qualifying wall samples",
                    "provenance": {"plan_fragment_ref": "fragment-ready"},
                }
            ],
        }),
        encoding="utf-8",
    )
    ready = PlanFragmentRecord(
        fragment_id="fragment-ready",
        snapshot_id="snapshot_v2",
        source_discovery_snapshot_id="discovery.snapshot.v2",
        capability_version="2.0.0:test",
        dependency_fingerprint="a" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=[],
        items=[GeometricRequest("feature-main-wall-1", ["G_WALL_IN_DRAFT"], {})],
    )
    waiting = PlanFragmentRecord(
        fragment_id="fragment-waiting",
        snapshot_id="snapshot_v2",
        source_discovery_snapshot_id="discovery.snapshot.v2",
        capability_version="2.0.0:test",
        dependency_fingerprint="b" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=["binding-draft"],
        items=[GeometricRequest("feature-main-wall-1", ["G_WALL_IN_DRAFT"], {})],
        status="waiting_input",
    )
    plan = PlanRecord(
        plan_id="plan-v2",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:00:00Z",
        plan_fragments=[ready, waiting],
    )
    result_artifact = ArtifactRecord(
        "artifact-results",
        "geometric_results",
        result_path.relative_to(tmp_path).as_posix(),
        "application/json",
        "2026-01-01T00:00:00Z",
    )

    artifact = JobManager._materialize_result_manifest(
        tmp_path, "run-v2", plan, [result_artifact]
    )
    payload = json.loads(
        (tmp_path / artifact.relative_path).read_text(encoding="utf-8")
    )
    assert payload["contract_version"] == "hermes.dfm.result-manifest/v1"
    assert payload["incomplete_fragments"] == [
        {
            "fragment_id": "fragment-waiting",
            "status": "waiting_input",
            "snapshot_id": "snapshot_v2",
            "rule_binding_refs": ["binding-draft"],
            "review_group_refs": [],
            "blocking_reasons": [],
        }
    ]
    assert payload["incomplete_results"][0]["status"] == "not_applicable"


@pytest.mark.skipif(not EXECUTABLE, reason="DFM_GEOMETRY_E2E_EXECUTABLE is unset")
def test_one_persistent_model_serves_discovery_and_direct_g_computation(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "dfm"
        / "step"
        / "injection_plate_with_hole.step"
    )
    project_dir = tmp_path / "project-v2"
    project_dir.mkdir()
    input_path = project_dir / "model.step"
    shutil.copy2(source, input_path)
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    input_record = InputRecord(
        input_id="input-v2",
        kind="step",
        source_name=input_path.name,
        relative_path=input_path.name,
        size_bytes=input_path.stat().st_size,
        sha256=input_sha256,
        created_at="2026-01-01T00:00:00Z",
    )
    pool = GeometrySessionPool(
        EXECUTABLE,
        request_timeout_seconds=30,
        job_timeout_seconds=120,
        poll_interval_seconds=0.02,
        idle_timeout_seconds=300,
        max_sessions=1,
        memory_budget_mb=4096,
    )
    try:
        discovery = OCCTCppFeatureRecognitionProvider(
            EXECUTABLE, session_pool=pool
        ).recognize(input_record, process="injection", project_dir=project_dir)
        assert discovery.worker_snapshot_id.startswith("snapshot_")
        assert not hasattr(discovery, "regions")
        assert all("region_refs" not in item.to_dict() for item in discovery.features)
        wall = next(item for item in discovery.features if item.kind == "main_wall")
        proposed_direction = [0.0, 0.0, 1.0]
        analyzed_scope = pool.call(
            "project-v2",
            "forming_scopes.analyze",
            {
                "snapshot_id": discovery.worker_snapshot_id,
                "forming_scope_id": "MAIN_POS",
                "mode": "main",
                "draw_direction": proposed_direction,
                "coordinate_system": "model",
                "direction_source": "user_confirmed",
            },
        )
        analyzed_scope = pool.wait_job("project-v2", str(analyzed_scope["job_id"]))
        candidate = next(
            item
            for item in analyzed_scope["forming_scopes"]
            if item["forming_scope_id"] == "MAIN_POS"
        )
        assert candidate["status"] == "candidate"
        confirmed_scope = pool.call(
            "project-v2",
            "forming_scopes.revise",
            {
                "snapshot_id": analyzed_scope["snapshot_id"],
                "scopes": [
                    {
                        "forming_scope_id": candidate["forming_scope_id"],
                        "mode": candidate["mode"],
                        "draw_direction": candidate["draw_direction"],
                        "coordinate_system": candidate["coordinate_system"],
                        "direction_source": candidate["direction_source"],
                        "geometry_refs": candidate["geometry_refs"],
                    }
                ],
            },
        )
        confirmed = next(
            item
            for item in confirmed_scope["forming_scopes"]
            if item["forming_scope_id"] == "MAIN_POS"
        )
        assert confirmed["status"] == "confirmed"
        opened = pool.acquire("project-v2").snapshots_by_input_sha256[input_sha256]
        pool.update_snapshot("project-v2", input_sha256, opened, confirmed_scope)
        forming_context = {
            "forming_scope_id": "MAIN_POS",
            "draw_direction": confirmed["draw_direction"],
            "coordinate_system": "model",
            "direction_source": "user_confirmed",
        }
        request = GeometricRequest(wall.feature_id, ["G_WALL_THK_MIN"], {})
        direction_request = GeometricRequest(
            wall.feature_id, ["G_WALL_IN_DRAFT"], forming_context
        )
        fragment = PlanFragmentRecord(
            fragment_id="fragment-v2",
            snapshot_id=confirmed_scope["snapshot_id"],
            source_discovery_snapshot_id="discovery.snapshot.v2",
            capability_version="test",
            dependency_fingerprint="a" * 64,
            confirmed_fact_refs=[],
            rule_binding_refs=[],
            items=[request, direction_request],
        )
        plan = PlanRecord(
            plan_id="plan-v2",
            input_mode="step",
            analyzer_keys=["occt_cpp"],
            status="ready",
            created_at="2026-01-01T00:00:00Z",
            plan_fragments=[fragment],
        )
        artifacts = OcctAnalyzer(EXECUTABLE, session_pool=pool).run(
            AnalyzerContext(
                "project-v2",
                project_dir,
                "step",
                [input_record],
                "run-v2",
                plan,
            ),
            CancellationToken(),
        )
        result_artifact = next(
            item for item in artifacts if item.kind == "geometric_results"
        )
        payload = json.loads(
            (project_dir / result_artifact.relative_path).read_text(encoding="utf-8")
        )
        assert payload["contract_version"] == "dfm.geometry/v2"
        assert len(payload["results"]) == 2
        result = next(
            item
            for item in payload["results"]
            if item["geometric_id"] == "G_WALL_THK_MIN"
        )
        assert result["geometric_id"] == "G_WALL_THK_MIN"
        assert result["feature_id"] == wall.feature_id
        assert result["status"] == "ok", json.dumps(result, ensure_ascii=False)
        assert set(result) == {
            "result_id",
            "snapshot_id",
            "feature_id",
            "geometric_id",
            "status",
            "value",
            "unit",
            "context",
            "evidence",
            "quality",
            "reason",
            "provenance",
            "input_sha256",
        }
        direction_result = next(
            item
            for item in payload["results"]
            if item["geometric_id"] == "G_WALL_IN_DRAFT"
        )
        # The flat fixture has no qualifying inner-wall draft sample, but the
        # confirmed context must be accepted (not rejected as missing or
        # ambiguous) and the unavailable value must stay explicit.
        assert direction_result["status"] in {"ok", "not_applicable"}
        assert direction_result["context"] == forming_context
        scalar = next(item for item in artifacts if item.kind == "scalar_field")
        scalar_payload = json.loads(
            (project_dir / scalar.relative_path).read_text(encoding="utf-8")
        )
        assert scalar_payload["contract_version"] == "dfm.geometry/v2"
        assert scalar_payload["snapshot_id"] == confirmed_scope["snapshot_id"]
        assert scalar_payload["feature_id"] == wall.feature_id
        assert scalar_payload["feature_refs"] == [wall.feature_id]
        assert "G_WALL_THK_MIN" in scalar_payload["geometric_ids"]
        assert not {
            "operation_id",
            "calculator_id",
            "metric_id",
            "metric_ids",
            "quantity_id",
            "region_refs",
        }.intersection(scalar_payload)
        cached = pool.acquire("project-v2").snapshots_by_input_sha256[input_sha256]
        assert cached["diagnostics"]["model_import_count"] == 1
        recovery_log = (
            project_dir
            / "artifacts"
            / "geometry_sessions"
            / input_sha256[:16]
            / "session-recovery.json"
        )
        recovery_payload = json.loads(recovery_log.read_text(encoding="utf-8"))
        assert [item["method"] for item in recovery_payload["steps"]] == [
            "features.discover",
            "features.discover",
            "snapshots.merge",
            "forming_scopes.analyze",
            "forming_scopes.revise",
        ]

        # Closing the Worker invalidates every in-memory handle.  The next run
        # must reopen the STEP, replay and verify discovery, map the pinned
        # logical snapshot to its new live handle, and compute without silently
        # reusing the dead handle.
        pool.close_project("project-v2")
        recovered_artifacts = OcctAnalyzer(EXECUTABLE, session_pool=pool).run(
            AnalyzerContext(
                "project-v2",
                project_dir,
                "step",
                [input_record],
                "run-v2-recovered",
                plan,
            ),
            CancellationToken(),
        )
        recovered_result = next(
            item for item in recovered_artifacts if item.kind == "geometric_results"
        )
        recovered_payload = json.loads(
            (project_dir / recovered_result.relative_path).read_text(encoding="utf-8")
        )
        assert {
            item["geometric_id"]: item["status"]
            for item in recovered_payload["results"]
        } == {item["geometric_id"]: item["status"] for item in payload["results"]}
        recovered_cache = pool.acquire("project-v2").snapshots_by_input_sha256[
            input_sha256
        ]
        assert recovered_cache["diagnostics"]["model_import_count"] == 1
    finally:
        pool.close()


@pytest.mark.skipif(not EXECUTABLE, reason="DFM_GEOMETRY_E2E_EXECUTABLE is unset")
def test_service_compiles_only_ready_v2_fragments_after_scope_review(tmp_path):
    token = set_hermes_home_override(tmp_path / "home")
    service = DFMService(
        config=DFMConfig(
            geometry_backend="occt_cpp_external",
            geometry_executable=EXECUTABLE,
            geometry_timeout_seconds=120,
            geometry_job_poll_interval_ms=20,
        ),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="v2 service flow")["project_id"]
        source = (
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "dfm"
            / "step"
            / "injection_plate_with_hole.step"
        )
        service.project("add_input", project_id=project_id, path=str(source))
        for name, value in {
            "process": "injection",
            "model_units": "mm",
            "pull_dir": [0, 0, 1],
            "material": "ABS",
        }.items():
            service.project(
                "confirm_fact",
                project_id=project_id,
                fact_name=name,
                fact_value=value,
            )

        discovery = service.analysis("discover", project_id=project_id)
        assert discovery["status"] == "discovery_review_required"
        wall_candidate = next(
            item for item in discovery["features"] if item["kind"] == "main_wall"
        )
        revised_feature = service.analysis(
            "update_feature_geometry",
            project_id=project_id,
            feature_id=wall_candidate["feature_id"],
            geometry_refs=wall_candidate["properties"]["geometry_refs"],
            expected_revision=discovery["revision"],
        )
        service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=revised_feature["revision"],
        )
        scope = service.analysis(
            "analyze_forming_scope",
            project_id=project_id,
            forming_scope_id="MAIN_POS",
            scope_mode="main",
            draw_direction=[0, 0, 1],
        )
        candidate = scope["forming_scope"]
        service.analysis(
            "revise_forming_scopes",
            project_id=project_id,
            forming_scopes=[
                {
                    key: candidate[key]
                    for key in (
                        "forming_scope_id",
                        "mode",
                        "draw_direction",
                        "coordinate_system",
                        "direction_source",
                        "geometry_refs",
                    )
                }
            ],
        )
        local = service.analysis(
            "analyze_forming_scope",
            project_id=project_id,
            forming_scope_id="SIDE_01",
            scope_mode="local",
            draw_direction=[1, 0, 0],
        )
        local_candidate = local["forming_scope"]
        undercut_group = next(
            item
            for item in local["review_groups"]
            if item["kind"] == "undercut_scope"
        )
        supplemented = service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=local["revision"],
            review_group_ids=[undercut_group["review_group_id"]],
        )
        pending_by_kind = {
            item["kind"]: item for item in supplemented["review_groups"]
        }
        assert {"local_structure", "local_direction"}.issubset(pending_by_kind)
        assert supplemented["next_action"] == "confirm_discovery"
        structure_confirmed = service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=supplemented["revision"],
            review_group_ids=[
                pending_by_kind["local_structure"]["review_group_id"]
            ],
        )
        assert structure_confirmed["status"] == "forming_scope_review_required"
        assert structure_confirmed["next_action"] == "revise_forming_scopes"
        service.analysis(
            "revise_forming_scopes",
            project_id=project_id,
            forming_scopes=[
                {
                    key: local_candidate[key]
                    for key in (
                        "forming_scope_id",
                        "mode",
                        "draw_direction",
                        "coordinate_system",
                        "direction_source",
                        "geometry_refs",
                    )
                }
            ],
        )
        planned = service.analysis("plan", project_id=project_id)
        plan = planned["plan"]
        assert plan["status"] == "ready"
        assert "operations" not in plan
        assert "regions" not in plan
        assert "affected_operation_ids" not in plan
        assert plan["plan_fragments"]
        assert all(item["status"] == "ready" for item in plan["plan_fragments"])
        serialized = json.dumps(plan, sort_keys=True)
        for forbidden in (
            "worker_geometric_id",
            "operation_id",
            "calculator_id",
            "metric_id",
            "metric_ids",
            "quantity_id",
            "face_indices",
            "region_refs",
            "task_id",
        ):
            assert f'"{forbidden}"' not in serialized

        started = service.analysis(
            "start", project_id=project_id, plan_id=plan["plan_id"]
        )
        run_id = started["run"]["run_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = service.analysis("status", project_id=project_id, run_id=run_id)[
                "run"
            ]
            if status["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert status["status"] == "reporting"
        manifest = service.project("status", project_id=project_id)["project"]
        run_artifacts = [
            item for item in manifest["artifacts"] if item.get("run_id") == run_id
        ]
        assert any(item["kind"] == "result_manifest" for item in run_artifacts)
        assert any(item["kind"] == "report_json" for item in run_artifacts)
        assert any(item["kind"] == "dfm_viewer" for item in run_artifacts)
        assert any(item["kind"] == "report_html_runtime" for item in run_artifacts)
    finally:
        service.close()
        reset_hermes_home_override(token)
