from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
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
    GeometricRuleOperand,
    InputRecord,
    PlanFragmentRecord,
    PlanRecord,
)
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.findings import materialize_evaluated_findings
from tools.dfm.config import DFMConfig
from tools.dfm.feature_recognition.occt_cpp import OCCTCppFeatureRecognitionProvider
from tools.dfm.runtime.process import (
    GeometrySessionPool,
    geometry_capability_identity,
)
from tools.dfm.runtime.jobs import JobManager
from tools.dfm.service import DFMService


EXECUTABLE = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE", "")


def test_screw_boss_wall_ratio_uses_two_public_g_results_for_one_finding(tmp_path):
    feature_id = "BOSS_0007"
    fragment_id = "fragment-screw-boss-wall"
    binding = GeometricRuleBinding(
        binding_id="binding-screw-boss-wall",
        geometric_id="G_HOLLOW_BOSS_WALL_THK_MAX",
        rule_id="R_SCREW_BOSS_WALL_THK_001",
        operator="<=",
        aggregation="identity",
        feature_refs=[feature_id],
        check_id="C_HOLLOW_BOSS_WALL_THK",
        operand_alias="boss_wall_thickness",
        additional_operands=[
            GeometricRuleOperand(
                alias="adjacent_main_wall_thickness",
                geometric_id="G_HOLLOW_BOSS_WALL_REF_THK",
                feature_refs=[feature_id],
            )
        ],
        expression={
            "op": "divide",
            "args": [
                {"operand": "boss_wall_thickness"},
                {"operand": "adjacent_main_wall_thickness"},
            ],
        },
    )
    plan = PlanRecord(
        plan_id="plan-screw-boss-wall",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:00:00Z",
        process="injection",
        rules={
            binding.rule_id: EffectiveRule(
                0.60,
                "ratio",
                "ontology:test",
            )
        },
        rule_bindings=[binding],
        plan_fragments=[
            PlanFragmentRecord(
                fragment_id=fragment_id,
                snapshot_id="snapshot-screw-boss",
                source_discovery_snapshot_id="discovery-screw-boss",
                capability_version="2.0.0:test",
                dependency_fingerprint="a" * 64,
                confirmed_fact_refs=[],
                rule_binding_refs=[binding.binding_id],
                items=[
                    GeometricRequest(
                        feature_id,
                        [
                            "G_HOLLOW_BOSS_WALL_THK_MAX",
                            "G_HOLLOW_BOSS_WALL_REF_THK",
                        ],
                        {},
                    )
                ],
            )
        ],
    )

    def result(result_id, geometric_id, value):
        return GeometricResultRecord(
            result_id=result_id,
            snapshot_id="snapshot-screw-boss",
            feature_id=feature_id,
            geometric_id=geometric_id,
            status="ok",
            value=value,
            unit="mm",
            context={},
            evidence={"geometry_refs": [], "field_refs": []},
            quality={"fixture": True, "algorithm_certified": False},
            reason=None,
            provenance={
                "source_snapshot_id": "snapshot-screw-boss",
                "source_result_ref": result_id,
                "dependency_fingerprint": "b" * 64,
                "plan_fragment_ref": fragment_id,
            },
            input_sha256="c" * 64,
        )

    results = [
        result(
            "result-boss-wall",
            "G_HOLLOW_BOSS_WALL_THK_MAX",
            2.1,
        ),
        result(
            "result-reference-wall",
            "G_HOLLOW_BOSS_WALL_REF_THK",
            3.0,
        ),
    ]
    evaluations, _ = EvaluationEngine().evaluate(results, plan)

    assert len(evaluations) == 1
    assert evaluations[0].actual == pytest.approx(0.70)
    assert evaluations[0].actual_unit == "ratio"
    assert evaluations[0].outcome == "fail"
    assert evaluations[0].measurement_ids == [
        "result-boss-wall",
        "result-reference-wall",
    ]
    serialized_plan = json.dumps(plan.to_dict(), sort_keys=True)
    for forbidden in {
        "worker_geometric_id",
        "operation_id",
        "calculator_id",
        "metric_id",
        "metric_ids",
        "quantity_id",
        "region_refs",
        "face_indices",
        "task_id",
    }:
        assert f'"{forbidden}"' not in serialized_plan

    output_dir = tmp_path / "runs" / "run-screw-boss" / "artifacts"
    output_dir.mkdir(parents=True)
    results_path = output_dir / "geometric_results.json"
    results_path.write_text(
        json.dumps(
            {
                "contract_version": "dfm.geometry/v2",
                "run_id": "run-screw-boss",
                "input_sha256": "c" * 64,
                "results": [item.to_dict() for item in results],
            }
        ),
        encoding="utf-8",
    )
    results_artifact = ArtifactRecord(
        "artifact-screw-boss-results",
        "geometric_results",
        results_path.relative_to(tmp_path).as_posix(),
        "application/json",
        "2026-01-01T00:00:00Z",
    )
    evaluations_artifact = EvaluationEngine().materialize(
        tmp_path,
        "run-screw-boss",
        plan,
        [results_artifact],
    )
    findings = materialize_evaluated_findings(
        tmp_path, [results_artifact, evaluations_artifact]
    )

    assert len(findings) == 1
    assert findings[0].check_ids == ["C_HOLLOW_BOSS_WALL_THK"]
    assert findings[0].feature_refs == [feature_id]
    assert findings[0].geometric_ids == [
        "G_HOLLOW_BOSS_WALL_REF_THK",
        "G_HOLLOW_BOSS_WALL_THK_MAX",
    ]


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


def test_followup_confirmation_creates_a_new_manifest_without_rewriting_stage_report(
    tmp_path,
):
    ready = PlanFragmentRecord(
        fragment_id="fragment-ordinary",
        snapshot_id="snapshot-stage-1",
        source_discovery_snapshot_id="discovery-stage-1",
        capability_version="2.0.0:test",
        dependency_fingerprint="a" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=["binding-ordinary"],
        items=[GeometricRequest("feature-wall", ["G_WALL_THK_MIN"], {})],
    )
    waiting = PlanFragmentRecord(
        fragment_id="fragment-side-waiting",
        snapshot_id="snapshot-stage-1",
        source_discovery_snapshot_id="discovery-stage-1",
        capability_version="2.0.0:test",
        dependency_fingerprint="b" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=["binding-side"],
        items=[
            GeometricRequest(
                "feature-wall",
                ["G_WALL_IN_DRAFT"],
                {"forming_scope_id": "SIDE_01"},
            )
        ],
        status="waiting_input",
        blocking_reasons=[{"code": "missing_context"}],
    )
    stage_plan = PlanRecord(
        plan_id="plan-stage-1",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:00:00Z",
        plan_fragments=[ready, waiting],
    )

    def result_artifact(run_id, results):
        path = tmp_path / "runs" / run_id / "artifacts" / "geometric_results.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {"contract_version": "dfm.geometry/v2", "results": results}
            ),
            encoding="utf-8",
        )
        return ArtifactRecord(
            f"artifact-{run_id}-results",
            "geometric_results",
            path.relative_to(tmp_path).as_posix(),
            "application/json",
            "2026-01-01T00:00:00Z",
        )

    ordinary_result = {
        "result_id": "result-ordinary",
        "snapshot_id": "snapshot-stage-1",
        "feature_id": "feature-wall",
        "geometric_id": "G_WALL_THK_MIN",
        "status": "ok",
        "reason": None,
        "provenance": {"plan_fragment_ref": ready.fragment_id},
    }
    first_artifact = JobManager._materialize_result_manifest(
        tmp_path,
        "run-stage-1",
        stage_plan,
        [result_artifact("run-stage-1", [ordinary_result])],
    )
    first_path = tmp_path / first_artifact.relative_path
    first_bytes = first_path.read_bytes()
    first_payload = json.loads(first_bytes)
    assert [
        item["fragment_id"] for item in first_payload["incomplete_fragments"]
    ] == [waiting.fragment_id]

    confirmed = PlanFragmentRecord(
        fragment_id="fragment-side-confirmed",
        snapshot_id="snapshot-stage-2",
        source_discovery_snapshot_id="discovery-stage-2",
        capability_version="2.0.0:test",
        dependency_fingerprint="c" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=["binding-side"],
        items=[
            GeometricRequest(
                "feature-wall",
                ["G_WALL_IN_DRAFT"],
                {
                    "forming_scope_id": "SIDE_01",
                    "draw_direction": [0.0, 1.0, 0.0],
                    "coordinate_system": "model",
                    "direction_source": "user_confirmed",
                },
            )
        ],
        supersedes=waiting.fragment_id,
    )
    followup_plan = PlanRecord(
        plan_id="plan-stage-2",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-01-01T00:01:00Z",
        parent_plan_id=stage_plan.plan_id,
        plan_fragments=[ready, waiting, confirmed],
    )
    side_result = {
        "result_id": "result-side",
        "snapshot_id": "snapshot-stage-2",
        "feature_id": "feature-wall",
        "geometric_id": "G_WALL_IN_DRAFT",
        "status": "ok",
        "reason": None,
        "provenance": {"plan_fragment_ref": confirmed.fragment_id},
    }
    second_artifact = JobManager._materialize_result_manifest(
        tmp_path,
        "run-stage-2",
        followup_plan,
        [result_artifact("run-stage-2", [ordinary_result, side_result])],
    )
    second_payload = json.loads(
        (tmp_path / second_artifact.relative_path).read_text(encoding="utf-8")
    )

    assert first_path.read_bytes() == first_bytes
    assert first_payload["run_id"] == "run-stage-1"
    assert second_payload["run_id"] == "run-stage-2"
    assert second_payload["incomplete_fragments"] == []
    assert second_payload["result_refs"] == ["result-ordinary", "result-side"]
    assert [
        item["fragment_id"] for item in second_payload["plan_fragments"]
    ] == [ready.fragment_id, confirmed.fragment_id]


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
        max_sessions=2,
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
        proposed_direction = [1.0, 0.0, 0.0]
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
        opened = pool.acquire("project-v2").snapshots_by_input_sha256[input_sha256]
        analyzed_scope = pool.update_snapshot(
            "project-v2", input_sha256, opened, analyzed_scope
        )
        candidate = next(
            item
            for item in analyzed_scope["forming_scopes"]
            if item["forming_scope_id"] == "MAIN_POS"
        )
        assert candidate["status"] == "candidate"
        local_candidate = next(
            item
            for item in analyzed_scope["forming_scopes"]
            if item["forming_scope_id"] == "SIDE_01"
        )
        assert local_candidate["mode"] == "local"
        assert local_candidate["draw_direction"] is None
        assert local_candidate["direction_source"] is None
        assert local_candidate["geometry_refs"]
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
        confirmed_scope = pool.update_snapshot(
            "project-v2", input_sha256, opened, confirmed_scope
        )
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
        shared_g_bindings = [
            GeometricRuleBinding(
                binding_id=f"binding-shared-{suffix}",
                geometric_id="G_WALL_THK_MIN",
                rule_id=f"rule-shared-{suffix}",
                operator=">=",
                aggregation="identity",
                feature_refs=[wall.feature_id],
            )
            for suffix in ("a", "b")
        ]
        worker_capability = pool.acquire("project-v2").session.capability
        fragment = PlanFragmentRecord(
            fragment_id="fragment-v2",
            snapshot_id=confirmed_scope["snapshot_id"],
            source_discovery_snapshot_id="discovery.snapshot.v2",
            capability_version=geometry_capability_identity(worker_capability),
            dependency_fingerprint=worker_capability["dependency_fingerprint"],
            confirmed_fact_refs=[],
            rule_binding_refs=[item.binding_id for item in shared_g_bindings],
            items=[request, direction_request],
        )
        plan = PlanRecord(
            plan_id="plan-v2",
            input_mode="step",
            analyzer_keys=["occt_cpp"],
            status="ready",
            created_at="2026-01-01T00:00:00Z",
            rules={
                "rule-shared-a": EffectiveRule(-1.0, "mm", "ontology:test-a"),
                "rule-shared-b": EffectiveRule(100.0, "mm", "ontology:test-b"),
            },
            rule_bindings=shared_g_bindings,
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
        assert sum(
            item["geometric_id"] == "G_WALL_THK_MIN"
            for item in payload["results"]
        ) == 1
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
        evaluations_artifact = EvaluationEngine().materialize(
            project_dir,
            "run-v2",
            plan,
            [result_artifact],
        )
        evaluations_payload = json.loads(
            (project_dir / evaluations_artifact.relative_path).read_text(
                encoding="utf-8"
            )
        )
        assert {
            item["rule_id"]: item["outcome"]
            for item in evaluations_payload["evaluations"]
        } == {"rule-shared-a": "pass", "rule-shared-b": "fail"}
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
        assert geometry_capability_identity(recovery_payload) == (
            geometry_capability_identity(worker_capability)
        )
        assert [item["method"] for item in recovery_payload["steps"]] == [
            "features.discover",
            "features.discover",
            "snapshots.merge",
            "forming_scopes.analyze",
            "forming_scopes.revise",
        ]
        assert all(
            len(item["result_state_sha256"]) == 64
            and set(item["result_state_sha256"]) <= set("0123456789abcdef")
            for item in recovery_payload["steps"]
        )

        other_project_dir = tmp_path / "project-v2-other"
        other_project_dir.mkdir()
        other_input_path = other_project_dir / "model.step"
        shutil.copy2(source, other_input_path)
        other_input = InputRecord(
            input_id="input-v2-other",
            kind="step",
            source_name=other_input_path.name,
            relative_path=other_input_path.name,
            size_bytes=other_input_path.stat().st_size,
            sha256=input_sha256,
            created_at="2026-01-01T00:00:00Z",
        )
        other_discovery = OCCTCppFeatureRecognitionProvider(
            EXECUTABLE, session_pool=pool
        ).recognize(
            other_input,
            process="injection",
            project_dir=other_project_dir,
        )
        other_session = pool.acquire("project-v2-other").session
        other_pid = other_session.pid
        assert other_discovery.worker_snapshot_id.startswith("snapshot_")

        # An unexpected Worker exit invalidates every in-memory handle owned by
        # this project.  The next run must reopen the STEP, replay and verify
        # discovery, map the pinned logical snapshot to its new live handle,
        # and compute without silently reusing the dead handle.
        crashed_session = pool.acquire("project-v2").session
        crashed_pid = crashed_session.pid
        crashed_session._process.terminate()
        crashed_session._process.wait(timeout=5)
        assert not crashed_session.alive
        assert pool.acquire("project-v2-other").session.pid == other_pid
        assert other_session.alive
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
        assert pool.acquire("project-v2").session.pid != crashed_pid
        assert recovered_cache["diagnostics"]["model_import_count"] == 1
    finally:
        pool.close()


def _fixture_worker_executable(tmp_path: Path) -> str:
    fixture = Path(__file__).resolve().parent / "fixtures" / "worker_fixture.py"
    if os.name == "nt":
        wrapper = tmp_path / "dfm-geometry-fixture.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{fixture}" session_v2 %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper = tmp_path / "dfm-geometry-fixture"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{fixture}" session_v2 "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return str(wrapper)


def test_ready_scopes_run_once_while_another_local_direction_waits(tmp_path):
    source = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "dfm"
        / "step"
        / "injection_plate_with_hole.step"
    )
    project_dir = tmp_path / "independent-scope-project"
    project_dir.mkdir()
    input_path = project_dir / "model.step"
    shutil.copy2(source, input_path)
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
    input_record = InputRecord(
        input_id="input-independent-scopes",
        kind="step",
        source_name=input_path.name,
        relative_path=input_path.name,
        size_bytes=input_path.stat().st_size,
        sha256=input_sha256,
        created_at="2026-01-01T00:00:00Z",
    )
    executable = _fixture_worker_executable(tmp_path)
    pool = GeometrySessionPool(
        executable,
        request_timeout_seconds=30,
        job_timeout_seconds=30,
        poll_interval_seconds=0.01,
        idle_timeout_seconds=300,
        max_sessions=1,
        memory_budget_mb=4096,
    )
    try:
        discovery = OCCTCppFeatureRecognitionProvider(
            executable, session_pool=pool
        ).recognize(input_record, process="injection", project_dir=project_dir)
        wall = next(item for item in discovery.features if item.kind == "main_wall")
        ready_direction = {
            "forming_scope_id": "SIDE_02",
            "draw_direction": [0.0, 1.0, 0.0],
            "coordinate_system": "model",
            "direction_source": "user_confirmed",
        }
        waiting_direction = {"forming_scope_id": "SIDE_01"}
        bindings = [
            GeometricRuleBinding(
                binding_id="binding-thickness-a",
                geometric_id="G_WALL_THK_MIN",
                rule_id="rule-thickness-a",
                operator=">=",
                aggregation="identity",
                feature_refs=[wall.feature_id],
                check_id="check-thickness-a",
            ),
            GeometricRuleBinding(
                binding_id="binding-thickness-b",
                geometric_id="G_WALL_THK_MIN",
                rule_id="rule-thickness-b",
                operator=">=",
                aggregation="identity",
                feature_refs=[wall.feature_id],
                check_id="check-thickness-b",
            ),
            GeometricRuleBinding(
                binding_id="binding-side-02",
                geometric_id="G_WALL_IN_DRAFT",
                rule_id="rule-side-02",
                operator=">=",
                aggregation="identity",
                feature_refs=[wall.feature_id],
                context=ready_direction,
                check_id="check-side-02",
            ),
            GeometricRuleBinding(
                binding_id="binding-side-01",
                geometric_id="G_WALL_IN_DRAFT",
                rule_id="rule-side-01",
                operator=">=",
                aggregation="identity",
                feature_refs=[wall.feature_id],
                context=waiting_direction,
                check_id="check-side-01",
            ),
        ]
        capability = pool.acquire(project_dir.name).session.capability
        common = {
            "snapshot_id": discovery.worker_snapshot_id,
            "source_discovery_snapshot_id": "discovery-independent-scopes",
            "capability_version": geometry_capability_identity(capability),
            "confirmed_fact_refs": [],
        }
        fragments = [
            PlanFragmentRecord(
                fragment_id="fragment-ordinary-ready",
                dependency_fingerprint="a" * 64,
                rule_binding_refs=["binding-thickness-a", "binding-thickness-b"],
                items=[
                    GeometricRequest(
                        wall.feature_id,
                        ["G_WALL_THK_MIN"],
                        {},
                    )
                ],
                **common,
            ),
            PlanFragmentRecord(
                fragment_id="fragment-side-02-ready",
                dependency_fingerprint="b" * 64,
                rule_binding_refs=["binding-side-02"],
                items=[
                    GeometricRequest(
                        wall.feature_id,
                        ["G_WALL_IN_DRAFT"],
                        ready_direction,
                    )
                ],
                **common,
            ),
            PlanFragmentRecord(
                fragment_id="fragment-side-01-waiting",
                dependency_fingerprint="c" * 64,
                rule_binding_refs=["binding-side-01"],
                items=[
                    GeometricRequest(
                        wall.feature_id,
                        ["G_WALL_IN_DRAFT"],
                        waiting_direction,
                    )
                ],
                status="waiting_input",
                blocking_reasons=[
                    {
                        "code": "missing_context",
                        "geometric_id": "G_WALL_IN_DRAFT",
                        "missing_fields": [
                            "coordinate_system",
                            "direction_source",
                            "draw_direction",
                        ],
                    }
                ],
                **common,
            ),
        ]
        plan = PlanRecord(
            plan_id="plan-independent-scopes",
            input_mode="step",
            analyzer_keys=["occt_cpp"],
            status="ready",
            created_at="2026-01-01T00:00:00Z",
            process="injection",
            rules={
                "rule-thickness-a": EffectiveRule(1.0, "mm", "fixture"),
                "rule-thickness-b": EffectiveRule(2.0, "mm", "fixture"),
                "rule-side-02": EffectiveRule(1.0, "degree", "fixture"),
                "rule-side-01": EffectiveRule(1.0, "degree", "fixture"),
            },
            rule_bindings=bindings,
            plan_fragments=fragments,
        )
        artifacts = OcctAnalyzer(executable, session_pool=pool).run(
            AnalyzerContext(
                project_dir.name,
                project_dir,
                "step",
                [input_record],
                "run-independent-scopes",
                plan,
            ),
            CancellationToken(),
        )
        results_artifact = next(
            item for item in artifacts if item.kind == "geometric_results"
        )
        results = json.loads(
            (project_dir / results_artifact.relative_path).read_text(encoding="utf-8")
        )["results"]
        assert {
            (item["geometric_id"], item["context"].get("forming_scope_id"))
            for item in results
        } == {
            ("G_WALL_THK_MIN", None),
            ("G_WALL_IN_DRAFT", "SIDE_02"),
        }
        assert sum(
            item["geometric_id"] == "G_WALL_THK_MIN" for item in results
        ) == 1

        evaluations = json.loads(
            (
                project_dir
                / EvaluationEngine().materialize(
                    project_dir,
                    "run-independent-scopes",
                    plan,
                    [results_artifact],
                ).relative_path
            ).read_text(encoding="utf-8")
        )["evaluations"]
        assert {item["rule_id"]: item["outcome"] for item in evaluations} == {
            "rule-thickness-a": "pass",
            "rule-thickness-b": "fail",
            "rule-side-02": "pass",
        }
    finally:
        pool.close()


def _assert_complete_service_flow(
    tmp_path: Path, executable: str, *, expect_fixture: bool
) -> None:
    token = set_hermes_home_override(tmp_path / "home")
    service = DFMService(
        config=DFMConfig(
            geometry_backend="occt_cpp_external",
            geometry_executable=executable,
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
            "pull_dir": [1, 0, 0],
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
        )
        candidate = scope["forming_scope"]
        assert candidate["forming_scope_id"] == "MAIN"
        local_candidate = next(
            item
            for item in scope["forming_scopes"]
            if item["forming_scope_id"] == "SIDE_01"
        )
        assert local_candidate["draw_direction"] is None
        assert local_candidate["direction_source"] is None
        assert local_candidate["geometry_refs"]
        undercut_group = next(
            item
            for item in scope["review_groups"]
            if item["kind"] == "undercut_scope"
        )
        main_revised = service.analysis(
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
        supplemented = service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=main_revised["revision"],
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
        local_revised = service.analysis(
            "revise_forming_scopes",
            project_id=project_id,
            forming_scopes=[
                {
                    "forming_scope_id": local_candidate["forming_scope_id"],
                    "mode": local_candidate["mode"],
                    "draw_direction": [0, 1, 0],
                    "coordinate_system": local_candidate["coordinate_system"],
                    "direction_source": "user_confirmed",
                    "geometry_refs": local_candidate["geometry_refs"],
                }
            ],
        )
        review_groups = service._store(project_id).load().review_groups
        assert [
            item.status
            for item in review_groups
            if item.kind == "product_forming" and "MAIN" in item.target_refs
        ] == ["confirmed"]
        assert {
            item.status
            for item in review_groups
            if item.kind == "local_direction" and "SIDE_01" in item.target_refs
        } == {"stale", "confirmed"}
        assert local_revised["forming_scopes"][0]["draw_direction"] == [0.0, 1.0, 0.0]
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
        artifacts_by_kind = {item["kind"]: item for item in run_artifacts}
        assert {
            "geometric_results",
            "evaluations",
            "result_manifest",
            "report_json",
            "report_markdown",
            "dfm_viewer",
            "report_html_runtime",
        }.issubset(artifacts_by_kind)
        project_dir = service.workspace.project_dir(project_id)
        result_manifest = json.loads(
            (
                project_dir
                / artifacts_by_kind["result_manifest"]["relative_path"]
            ).read_text(encoding="utf-8")
        )
        assert result_manifest["contract_version"] == "hermes.dfm.result-manifest/v1"
        assert result_manifest["result_refs"]
        assert result_manifest["incomplete_fragments"] == []
        viewer = json.loads(
            (project_dir / artifacts_by_kind["dfm_viewer"]["relative_path"]).read_text(
                encoding="utf-8"
            )
        )
        runtime = json.loads(
            (
                project_dir
                / artifacts_by_kind["report_html_runtime"]["relative_path"]
            ).read_text(encoding="utf-8")
        )
        assert viewer["run_id"] == runtime["report"]["run_id"] == run_id
        assert viewer["render_mesh_snapshot_id"] == (
            runtime["result_manifest"]["render_mesh_snapshot_id"]
        )
        assert viewer["topology_snapshot_id"] == (
            runtime["result_manifest"]["topology_snapshot_id"]
        )
        input_sha256 = manifest["inputs"][0]["sha256"]
        cached = service.geometry_sessions.acquire(
            project_id
        ).snapshots_by_input_sha256[input_sha256]
        assert cached["diagnostics"]["model_import_count"] == 1
        if expect_fixture:
            assert result_manifest["incomplete_results"] == []
            assert manifest["findings"]
            assert all(
                item["validity_status"] == "current"
                for item in manifest["findings"]
            )
            assert viewer["issue_count"] == len(manifest["findings"])
            assert viewer["analysis_coverage"]["status"] == "complete"
            geometric_payload = json.loads(
                (
                    project_dir
                    / artifacts_by_kind["geometric_results"]["relative_path"]
                ).read_text(encoding="utf-8")
            )
            assert geometric_payload["results"]
            assert all(
                item["quality"]
                == {
                    "fixture": True,
                    "certified": False,
                    "algorithm_certified": False,
                }
                for item in geometric_payload["results"]
            )

        report_context = service.analysis(
            "report_context",
            project_id=project_id,
            run_id=run_id,
        )
        assert report_context["ready"] is True
        render = service.analysis(
            "render_html",
            project_id=project_id,
            run_id=run_id,
            llm_content={
                "schema_version": "dfm-html-llm/v1",
                "part": {
                    "name": "v2 service flow",
                    "general_tolerance": "Not provided",
                    "technical_note": "Deterministic workflow acceptance data.",
                },
                "issues": [
                    {
                        "issue_id": issue_id,
                        "title": issue_id,
                        "description": "Review the deterministic DFM finding.",
                    }
                    for issue_id in report_context["required_issue_ids"]
                ],
                "conclusion": {
                    "assessment_level": "以确定性规则结果为准",
                    "summary": "The workflow completed with pinned deterministic results.",
                    "risks": [],
                    "actions": [],
                },
            },
        )
        assert render["accepted"] is True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            final_status = service.analysis(
                "status", project_id=project_id, run_id=run_id
            )["run"]
            if final_status["status"] != "reporting":
                break
            time.sleep(0.02)
        assert final_status["status"] == "succeeded"
        completed = service.analysis(
            "result", project_id=project_id, run_id=run_id
        )["run"]
        final_artifacts = {item["kind"]: item for item in completed["artifacts"]}
        assert "report_html" in final_artifacts
        assert final_artifacts["result_manifest"]["artifact_id"] == (
            artifacts_by_kind["result_manifest"]["artifact_id"]
        )
    finally:
        service.close()
        reset_hermes_home_override(token)


def test_complete_service_flow_with_deterministic_v2_worker_data(tmp_path):
    _assert_complete_service_flow(
        tmp_path,
        _fixture_worker_executable(tmp_path),
        expect_fixture=True,
    )


@pytest.mark.skipif(not EXECUTABLE, reason="DFM_GEOMETRY_E2E_EXECUTABLE is unset")
def test_service_compiles_only_ready_v2_fragments_after_scope_review(tmp_path):
    _assert_complete_service_flow(tmp_path, EXECUTABLE, expect_fixture=False)
