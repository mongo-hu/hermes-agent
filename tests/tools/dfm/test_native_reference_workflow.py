"""Real OCCT geometry with explicit fixture rules for reference scheduling."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from tools.dfm.config import DFMConfig
from tools.dfm.contracts import (
    EffectiveRule,
    GeometricRuleBinding,
    GeometricRuleOperand,
    PlanRecord,
)
from tools.dfm.processes.base import ProcessPlan
from tools.dfm.service import DFMService
from tools.dfm.errors import DFMError
from tools.dfm.geometry import artifact_metadata as metadata_cache
from tools.dfm.runtime import result_documents


EXECUTABLE = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE", "")
pytestmark = pytest.mark.skipif(
    not EXECUTABLE, reason="native Worker executable is not configured"
)


def assert_viewer_asset_closure(project_dir, manifest_path, payload=None):
    payload = payload or json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = {}
    for key in ("scene_path", "topology_path"):
        path = (manifest_path.parent / payload[key]).resolve()
        assert path.is_relative_to(project_dir.resolve())
        assert path.is_file(), (key, path)
        assets[key] = json.loads(path.read_text(encoding="utf-8"))
    scene = assets["scene_path"]
    topology = assets["topology_path"]
    assert (
        scene["render_mesh_snapshot"]["render_mesh_snapshot_id"]
        == topology["render_mesh_snapshot_ref"]
    )
    assert (
        scene["topology_snapshot_ref"]
        == topology["topology_snapshot"]["topology_snapshot_id"]
    )
    if payload.get("render_mesh_snapshot_id"):
        assert (
            payload["render_mesh_snapshot_id"]
            == scene["render_mesh_snapshot"]["render_mesh_snapshot_id"]
        )
    if payload.get("topology_snapshot_id"):
        assert (
            payload["topology_snapshot_id"]
            == topology["topology_snapshot"]["topology_snapshot_id"]
        )


@pytest.mark.parametrize(
    "mode",
    [
        "deferred",
        "batched",
        "fail_later",
        "cancel_later",
        "missing_result",
        "tampered_shared",
    ],
)
def test_reference_wall_review_unlocks_only_its_g_and_resumes_the_same_native_run(
    tmp_path, monkeypatch, mode
):
    metadata_cache.clear_geometry_file_cache()
    result_documents.clear_result_document_cache()
    header_reads = {}
    small_result_reads = {}
    parse_header = metadata_cache._parse_header
    read_result_entry = result_documents._read_entry

    def count_result_entry(path, kind, signature):
        small_result_reads[kind] = small_result_reads.get(kind, 0) + 1
        return read_result_entry(path, kind, signature)

    def count_header(content, kind):
        header_reads[kind] = header_reads.get(kind, 0) + 1
        return parse_header(content, kind)

    monkeypatch.setattr(metadata_cache, "_parse_header", count_header)
    monkeypatch.setattr(result_documents, "_read_entry", count_result_entry)
    fixtures = Path(EXECUTABLE).with_name(
        "dfm_geometry_tests" + Path(EXECUTABLE).suffix
    )
    if not fixtures.is_file():
        pytest.skip("native analytic model exporter is unavailable")
    models = tmp_path / "models"
    subprocess.run(
        [str(fixtures), "[v105-fixtures]", "--reporter", "compact"],
        env={**os.environ, "DFM_V105_FIXTURES": str(models)},
        capture_output=True,
        check=True,
        timeout=90,
    )
    own = GeometricRuleBinding(
        "fixture.own",
        "G_HOLLOW_BOSS_WALL_THK_MAX",
        "fixture.own.rule",
        "<=",
        "identity",
        check_id="C_FIXTURE_BOSS_WALL",
        operand_alias="actual",
    )
    ratio = GeometricRuleBinding(
        "fixture.ratio",
        "G_HOLLOW_BOSS_WALL_THK_MAX",
        "fixture.ratio.rule",
        "<=",
        "identity",
        check_id="C_FIXTURE_BOSS_RATIO",
        operand_alias="actual",
        additional_operands=[
            GeometricRuleOperand(
                "reference", "G_HOLLOW_BOSS_WALL_REF_THK", feature_refs=[]
            )
        ],
        expression={
            "op": "divide",
            "args": [{"operand": "actual"}, {"operand": "reference"}],
        },
    )
    rules = ProcessPlan(
        "injection",
        "fixture.reference.scheduler",
        "injection.geometry-core",
        "fixture",
        {
            own.rule_id: EffectiveRule(3.0, "mm", "fixture:not_production"),
            ratio.rule_id: EffectiveRule(0.6, "ratio", "fixture:not_production"),
        },
        [],
        {"step"},
        rule_bindings=[own, ratio],
        binding_selectors={
            own.binding_id: {"actual": {"feature_kind": "hollow_boss"}},
            ratio.binding_id: {
                "actual": {"feature_kind": "hollow_boss"},
                "reference": {"feature_kind": "hollow_boss"},
            },
        },
    )
    if mode != "deferred":
        hole = GeometricRuleBinding(
            "fixture.hole",
            "G_HOLLOW_BOSS_HOLE_DEPTH",
            "fixture.hole.rule",
            "<=",
            "identity",
            check_id="C_FIXTURE_BOSS_HOLE",
            operand_alias="actual",
        )
        rules = replace(
            rules,
            rules={
                **rules.rules,
                hole.rule_id: EffectiveRule(1.0, "mm", "fixture:not_production"),
            },
            rule_bindings=[*rules.rule_bindings, hole],
            binding_selectors={
                **rules.binding_selectors,
                hole.binding_id: {"actual": {"feature_kind": "hollow_boss"}},
            },
        )
    monkeypatch.setattr(
        "tools.dfm.service.compile_occt_injection_plan", lambda *_args, **_kwargs: rules
    )
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
        project_id = service.project("create", name="native reference workflow")[
            "project_id"
        ]
        uploaded = service.project(
            "add_input", project_id=project_id, path=str(models / "rounded_boss.step")
        )
        assert uploaded["preview"]["status"] == "ready"
        assert_viewer_asset_closure(
            service.workspace.project_dir(project_id),
            Path(uploaded["preview"]["viewer_manifest"]),
        )
        for name, value in {
            "process": "injection",
            "model_units": "mm",
            "pull_dir": [0, 0, 1],
            "material": "ABS",
        }.items():
            service.project(
                "confirm_fact", project_id=project_id, fact_name=name, fact_value=value
            )
        service.analysis("discover", project_id=project_id)
        context = service.analysis("review_context", project_id=project_id)
        project_dir = service.workspace.project_dir(project_id)
        discovery_viewer = next(
            project_dir.glob("artifacts/geometry_sessions/*/dfm_discovery_viewer.json")
        )
        assert_viewer_asset_closure(project_dir, discovery_viewer)
        boss = next(row for row in context["features"] if row["kind"] == "hollow_boss")
        relation = next(
            row
            for row in context["relationships"]
            if row["type"] == "adjacent_main_wall"
            and row["from_ref"] == boss["feature_id"]
        )
        boss_group = next(
            group
            for group in context["review_groups"]
            if group["kind"] == "ordinary_structure"
            and boss["feature_id"] in group["target_refs"]
        )
        wall_group = next(
            group
            for group in context["review_groups"]
            if group["kind"] == "ordinary_structure"
            and relation["to_ref"] in group["target_refs"]
        )
        service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=context["revision"],
            review_group_ids=[
                boss_group["review_group_id"],
                *([wall_group["review_group_id"]] if mode != "deferred" else []),
            ],
        )
        planned = service.analysis("plan", project_id=project_id)
        assert "plan" in planned, planned
        plan = PlanRecord.from_dict(planned["plan"])
        ready = plan.active_plan_fragments(statuses={"ready"})
        pending = plan.active_plan_fragments(statuses={"pending_review"})
        assert any(
            "G_HOLLOW_BOSS_WALL_THK_MAX" in item.geometric_ids
            for fragment in ready
            for item in fragment.items
        )
        assert any(
            "G_HOLLOW_BOSS_WALL_REF_THK" in item.geometric_ids
            for fragment in ready
            for item in fragment.items
        ) == (mode != "deferred")
        if mode == "deferred":
            reference = next(
                fragment
                for fragment in pending
                if any(
                    "G_HOLLOW_BOSS_WALL_REF_THK" in item.geometric_ids
                    for item in fragment.items
                )
            )
            assert wall_group["review_group_id"] in reference.review_group_refs
            assert (
                reference.reference_dependencies[0]["target_feature_id"]
                == relation["to_ref"]
            )
        submitted_gs = []
        submitted_batches = []
        first_batch_proof = {}
        original_call = service.geometry_sessions.call

        def observe_call(pid, method, params):
            if method == "geometrics.compute":
                if mode != "deferred" and submitted_batches:
                    # The second RPC must start only after the first viewer is committed.
                    current = service._store(pid).load().runs[-1]
                    assert current.completed_fragment_refs
                    viewer = next(
                        row for row in current.artifacts if row.kind == "dfm_viewer"
                    )
                    viewer_data = json.loads(
                        (
                            service.workspace.project_dir(pid) / viewer.relative_path
                        ).read_text(encoding="utf-8")
                    )
                    assert viewer_data["issues"]
                    assert_viewer_asset_closure(
                        service.workspace.project_dir(pid),
                        service.workspace.project_dir(pid) / viewer.relative_path,
                        viewer_data,
                    )
                    assert viewer_data["status"] == "partial"
                    assert viewer_data["analysis_coverage"]["status"] == "partial"
                    # Evaluation/evidence/Viewer share the in-memory tables.
                    # Only final sealing may force a fresh content read.
                    assert not small_result_reads
                    assert not any(
                        row.kind == "report_html_runtime" for row in current.artifacts
                    )
                    first_batch_proof.update({
                        "completed": set(current.completed_fragment_refs),
                        "viewer": viewer,
                        "artifacts": [
                            row
                            for row in current.artifacts
                            if row.kind == "geometric_batch"
                        ],
                    })
                    if mode == "fail_later":
                        raise DFMError(
                            "fixture_second_batch_failed",
                            "Explicit transport failure after a real successful batch",
                        )
                    if mode == "cancel_later":
                        service.analysis(
                            "cancel", project_id=pid, run_id=current.run_id
                        )
                        raise DFMError(
                            "run_cancelled",
                            "Explicit user cancellation between batches",
                        )
                submitted_batches.append(params)
                submitted_gs.extend(
                    geometric_id
                    for item in params["items"]
                    for geometric_id in item["geometric_ids"]
                )
            return original_call(pid, method, params)

        monkeypatch.setattr(service.geometry_sessions, "call", observe_call)
        original_wait = service.geometry_sessions.wait_job

        def incomplete_batch(*args, **kwargs):
            result = original_wait(*args, **kwargs)
            if mode == "missing_result" and len(submitted_batches) > 1:
                return {**result, "results": result["results"][:-1]}
            if mode == "tampered_shared" and len(submitted_batches) > 1:
                manifest = service._store(project_id).load()
                owned = service.geometry_sessions.acquire(project_id)
                opened = owned.snapshots_by_input_sha256[manifest.inputs[0].sha256]
                path = Path(opened["artifacts"]["preflight"])
                stamp = path.stat()
                fixed_signature = metadata_cache._stat_signature(stamp)
                actual_signature = metadata_cache._stat_signature

                def frozen_signature(value):
                    return (
                        fixed_signature
                        if (value.st_dev, value.st_ino) == (stamp.st_dev, stamp.st_ino)
                        else actual_signature(value)
                    )

                monkeypatch.setattr(metadata_cache, "_stat_signature", frozen_signature)
                content = path.read_bytes()
                changed = content.replace(b" ", b"\t", 1)
                assert changed != content
                path.write_bytes(changed)
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            return result

        monkeypatch.setattr(service.geometry_sessions, "wait_job", incomplete_batch)
        run_id = service.analysis("start", project_id=project_id, plan_id=plan.plan_id)[
            "run"
        ]["run_id"]

        def wait(stages):
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
            pytest.fail(f"native reference Run remained {run.status.value}/{run.stage}")

        parked = None
        if mode == "deferred":
            parked = wait({"waiting_review"})
            assert parked.stage == "waiting_review", parked
            assert parked.completed_fragment_refs
            assert not any(
                artifact.kind == "report_html_runtime" for artifact in parked.artifacts
            )
            context = service.analysis("review_context", project_id=project_id)
            service.analysis(
                "confirm_discovery",
                project_id=project_id,
                expected_revision=context["revision"],
                review_group_ids=[wall_group["review_group_id"]],
            )
        finished = wait({"report_editing", "complete"})
        if mode == "tampered_shared":
            assert finished.status.value == "failed", finished
            assert finished.error["code"] == "artifact_invalid"
            assert len(finished.completed_fragment_refs) == len(
                plan.active_plan_fragments(statuses={"ready"})
            )
            assert not any(
                row.kind == "report_html_runtime" for row in finished.artifacts
            )
            return
        if mode in {"fail_later", "cancel_later", "missing_result"}:
            assert finished.status.value == (
                "cancelled" if mode == "cancel_later" else "failed"
            ), finished
            assert (
                set(finished.completed_fragment_refs) == first_batch_proof["completed"]
            )
            assert len(submitted_batches) == (2 if mode == "missing_result" else 1)
            assert first_batch_proof["artifacts"]
            for artifact in [
                first_batch_proof["viewer"],
                *first_batch_proof["artifacts"],
            ]:
                assert (
                    hashlib.sha256(
                        (
                            service.workspace.project_dir(project_id)
                            / artifact.relative_path
                        ).read_bytes()
                    ).hexdigest()
                    == artifact.sha256
                )
            return
        assert finished.stage in {"report_editing", "complete"}, finished
        if parked is not None:
            assert set(parked.completed_fragment_refs) <= set(
                finished.completed_fragment_refs
            )
        else:
            assert first_batch_proof
            assert len(submitted_batches) == 2
            assert any(
                {"G_HOLLOW_BOSS_WALL_THK_MAX", "G_HOLLOW_BOSS_WALL_REF_THK"}
                <= set(item["geometric_ids"])
                for batch in submitted_batches
                for item in batch["items"]
            )
        project_dir = service.workspace.project_dir(project_id)
        final_plan = PlanRecord.from_dict(finished.plan_snapshot)
        active_ids = {
            fragment.fragment_id for fragment in final_plan.active_plan_fragments()
        }
        artifact = next(
            item
            for item in finished.artifacts
            if item.kind == "geometric_results"
            and not item.relative_path.endswith(".incremental.json")
        )
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
        results = [
            row
            for row in payload["results"]
            if row["provenance"]["plan_fragment_ref"] in active_ids
        ]
        own_result = next(
            row for row in results if row["geometric_id"] == "G_HOLLOW_BOSS_WALL_THK_MAX"
        )
        reference_result = next(
            row for row in results if row["geometric_id"] == "G_HOLLOW_BOSS_WALL_REF_THK"
        )
        # Unchanged completed fragments keep their original result binding;
        # they need neither a new Worker request nor a cross-snapshot cache flag.
        if parked is not None:
            assert (
                own_result["provenance"]["plan_fragment_ref"]
                in parked.completed_fragment_refs
            )
        assert (
            own_result["provenance"]["plan_fragment_ref"]
            != reference_result["provenance"]["plan_fragment_ref"]
        )
        assert submitted_gs.count("G_HOLLOW_BOSS_WALL_THK_MAX") == 1
        assert submitted_gs.count("G_HOLLOW_BOSS_WALL_REF_THK") == 1
        assert own_result["status"] == "ok", own_result.get("reason")
        assert reference_result["status"] == "ok", reference_result.get("reason")
        assert own_result["value"] / reference_result["value"] == pytest.approx(2 / 3)
        evaluation = next(
            item for item in finished.artifacts if item.kind == "evaluations"
        )
        verdicts = json.loads(
            (project_dir / evaluation.relative_path).read_text(encoding="utf-8")
        )["evaluations"]
        assert any(
            row["check_id"] == ratio.check_id and row["outcome"] == "fail"
            for row in verdicts
        )
        manifest = service._store(project_id).load()
        state = manifest.capabilities["geometry_discovery"]["inputs"][
            manifest.inputs[0].sha256
        ]
        worker_state = service.geometry_sessions.call(
            project_id,
            "snapshots.get",
            {
                "snapshot_id": service.geometry_sessions.resolve_snapshot_id(
                    project_id, state["worker_snapshot_id"]
                )
            },
        )
        assert worker_state["diagnostics"]["model_import_count"] == 1
        assert header_reads == {"render_scene": 1, "topology_map": 1}
        assert small_result_reads == {"geometric_results": 1}
    finally:
        service.close()
        metadata_cache.clear_geometry_file_cache()
        result_documents.clear_result_document_cache()
