import json
from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from tools.dfm.contracts import (
    ArtifactRecord,
    InputRecord,
    PlanRecord,
    RunRecord,
    RunStatus,
)
from tools.dfm.errors import DFMError
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.reporting.html import materialize_html_runtime, render_html_report


def _artifact(
    project_dir: Path, run_id: str, name: str, kind: str, payload
) -> ArtifactRecord:
    path = project_dir / "runs" / run_id / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return ArtifactRecord(
        f"artifact_{name}",
        kind,
        path.relative_to(project_dir).as_posix(),
        "application/json",
        "now",
    )


def _drawing_observations(
    project_dir: Path, drawing: InputRecord, rows: list[dict]
) -> ArtifactRecord:
    path = (
        project_dir
        / "discovery"
        / "drawing"
        / drawing.sha256[:16]
        / f"drawing_{drawing.sha256[:16]}_agent_observations.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return ArtifactRecord(
        "artifact_drawing_observations",
        "drawing_observations",
        path.relative_to(project_dir).as_posix(),
        "application/x-ndjson",
        "now",
        logical_id=f"drawing-observations:{drawing.input_id}:1.0.0",
    )


def _html_contract_fixture(root: Path, run_id: str = "run_fixture") -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    issue_id = "evaluation-draft"
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "input_sha256": "a" * 64,
        "process": "injection",
        "scope_id": "injection.default",
        "scope_version": "1.1.0",
        "producer": "hermes-result-assembler-v1",
        "producer_contract": "evaluated_objective_result",
        "stats": {
            "measurement_count": 1,
            "evaluation_count": 1,
            "failed_count": 1,
        },
        "issues": [
            {
                "id": issue_id,
                "code": "injection.geometry.draft",
                "title": "Draft check",
                "severity": "unclassified",
                "message": "Actual 0.0 does not satisfy >= 1.0.",
                "metric": {
                    "actual": 0.0,
                    "expected": 1.0,
                    "operator": ">=",
                    "rule_id": "R_DRAFT",
                    "rule_version": "1.0.0",
                    "rule_hash": "b" * 64,
                    "measurement_ids": ["measurement-draft"],
                    "backend": "test",
                    "certified": False,
                    "algorithm_version": "test-1.0",
                },
                "images": ["evidence_001.png"],
                "image": "evidence_001.png",
                "feature_refs": ["feature.main-wall"],
                "region_refs": ["region.main-wall"],
                "recommendation": "Correct the geometry and rerun.",
            }
        ],
    }
    payloads = {
        "dfm_report.json": report,
        "render_scene.json": {"schema_version": 2, "primitives": []},
        "scalar_field_wall_thickness.json": {
            "metric_id": "injection.geometry.wall_thickness",
            "samples": [],
        },
        "scalar_field_draft.json": {
            "metric_id": "injection.geometry.draft",
            "samples": [],
        },
        "evidence_geometry.json": {"failed_patches": []},
        "rule_library.json": {"schema_version": 1, "rules": {}},
    }
    for name, payload in payloads.items():
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    (root / "evidence_001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "drawing.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    runtime = {
        "schema_version": "dfm-html-runtime/v1",
        "display": {
            "process": "注塑 (Injection)",
            "rule_scope": "injection.default@1.1.0",
        },
        "report": report,
        "drawing_semantics": {"observations": []},
        "global_issue_metadata": [],
        "model_metrics": [{"label": "未通过项总数", "value": 1}],
        "resources": {
            "evidence_root": ".",
            "scene_path": "render_scene.json",
            "scalar_fields": {
                "thickness": "scalar_field_wall_thickness.json",
                "draft": "scalar_field_draft.json",
            },
            "evidence_geometry_path": "evidence_geometry.json",
            "drawing_pdf_path": "drawing.pdf",
            "rule_library_path": "rule_library.json",
        },
    }
    llm = {
        "schema_version": "dfm-html-llm/v1",
        "part": {
            "name": "测试零件",
            "general_tolerance": "两位小数 ±0.100 mm",
            "technical_note": "边缘按 ISO 13715 执行。",
        },
        "issues": [
            {
                "issue_id": issue_id,
                "title": "主壁拔模角不足",
                "description": "实测 0.0°，未满足 ≥1.0° 要求。",
            }
        ],
        "conclusion": {
            "assessment_level": "需要改进",
            "summary": "检出 1 项未通过项。",
            "risks": [{"title": "脱模风险", "description": "需要工程复核。"}],
            "actions": [{"title": "优化拔模", "description": "修正后重跑同一计划。"}],
        },
    }
    runtime_path = root / "runtime_data.jsonl"
    llm_path = root / "llm_content.jsonl"
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False) + "\n", encoding="utf-8")
    llm_path.write_text(json.dumps(llm, ensure_ascii=False) + "\n", encoding="utf-8")
    return llm_path, runtime_path


def test_runtime_adapter_maps_existing_dfm_artifacts_without_recomputation(tmp_path):
    run_id = "run_1"
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "input_sha256": "a" * 64,
        "process": "injection",
        "scope_id": "injection.default",
        "scope_version": "1.1.0",
        "producer": "hermes-result-assembler-v1",
        "producer_contract": "evaluated_objective_result",
        "stats": {
            "measurement_count": 2,
            "evaluation_count": 2,
            "failed_count": 1,
        },
        "issues": [
            {
                "id": "evaluation-draft",
                "code": "injection.geometry.draft",
                "severity": "unclassified",
                "images": ["evidence_001.png"],
                "image": "evidence_001.png",
            }
        ],
    }
    artifacts = [
        _artifact(tmp_path, run_id, "dfm_report.json", "report_json", report),
        _artifact(
            tmp_path,
            run_id,
            "render_scene.json",
            "render_scene",
            {"schema_version": 2, "primitives": []},
        ),
        _artifact(
            tmp_path,
            run_id,
            "scalar_field_wall_thickness.json",
            "scalar_field",
            {"metric_id": "injection.geometry.wall_thickness", "samples": []},
        ),
        _artifact(
            tmp_path,
            run_id,
            "scalar_field_draft.json",
            "scalar_field",
            {"metric_id": "injection.geometry.draft", "samples": []},
        ),
        _artifact(
            tmp_path,
            run_id,
            "evidence_geometry.json",
            "evidence_geometry",
            {"failed_patches": []},
        ),
        _artifact(tmp_path, run_id, "evidence_001.png", "evidence_image", b"png"),
    ]
    drawing_path = tmp_path / "inputs" / "drawing.pdf"
    drawing_path.parent.mkdir(parents=True)
    drawing_path.write_bytes(b"pdf")
    drawing = InputRecord(
        "input_drawing_1",
        "drawing",
        "drawing.pdf",
        drawing_path.relative_to(tmp_path).as_posix(),
        drawing_path.stat().st_size,
        "b" * 64,
        "now",
        format_id="drawing",
        representation="document",
    )
    plan = PlanRecord(
        "plan_1",
        "fusion",
        ["step"],
        "ready",
        "now",
        process="injection",
        scope_id="injection.default",
        scope_version="1.1.0",
        input_ids=[drawing.input_id],
    )
    observations = [
        {
            "observation_id": "observation.agent.note-1",
            "input_id": drawing.input_id,
            "kind": "global_note",
            "value": "Edges and undimensioned details per ISO 13715.",
            "unit": None,
            "confidence": 0.98,
            "status": "candidate",
            "source_refs": ["artifact:ocr#fragment=note-1"],
            "region_refs": [],
            "feature_refs": [],
            "provenance": {"pages": [1]},
        }
    ]
    stale_observation = {
        **observations[0],
        "observation_id": "observation.agent.stale-note",
        "value": "A stale observation not pinned by this plan.",
    }
    observation_artifact = _drawing_observations(
        tmp_path, drawing, [*observations, stale_observation]
    )

    generated = materialize_html_runtime(
        tmp_path,
        run_id,
        plan,
        [drawing],
        artifacts,
        semantic_artifacts=[observation_artifact],
        observation_refs={"observation.agent.note-1"},
    )

    assert generated is not None
    runtime_path = tmp_path / generated.relative_path
    rows = [
        line
        for line in runtime_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(rows) == 1
    runtime = json.loads(rows[0])
    assert runtime["report"] == report
    assert runtime["drawing_semantics"] == {
        "input_id": drawing.input_id,
        "input_sha256": drawing.sha256,
        "source_artifact_id": observation_artifact.artifact_id,
        "observations": observations,
    }
    assert runtime["resources"]["evidence_root"] == "."
    assert runtime["resources"]["scalar_fields"] == {
        "thickness": "scalar_field_wall_thickness.json",
        "draft": "scalar_field_draft.json",
    }
    assert runtime["resources"]["scene_path"] == "render_scene.json"
    assert runtime["resources"]["evidence_geometry_path"] == "evidence_geometry.json"
    assert runtime["resources"]["drawing_pdf_path"].endswith("inputs/drawing.pdf")
    assert runtime["resources"]["drawing_observations_path"].endswith(
        "drawing_bbbbbbbbbbbbbbbb_agent_observations.jsonl"
    )


def test_runtime_adapter_requires_persisted_drawing_semantics(tmp_path):
    run_id = "run_1"
    artifacts = [
        _artifact(tmp_path, run_id, "dfm_report.json", "report_json", {}),
        _artifact(tmp_path, run_id, "render_scene.json", "render_scene", {}),
        _artifact(
            tmp_path,
            run_id,
            "evidence_geometry.json",
            "evidence_geometry",
            {},
        ),
        _artifact(
            tmp_path,
            run_id,
            "scalar_field_wall_thickness.json",
            "scalar_field",
            {"metric_id": "injection.geometry.wall_thickness"},
        ),
        _artifact(
            tmp_path,
            run_id,
            "scalar_field_draft.json",
            "scalar_field",
            {"metric_id": "injection.geometry.draft"},
        ),
    ]
    drawing_path = tmp_path / "inputs" / "drawing.pdf"
    drawing_path.parent.mkdir(parents=True)
    drawing_path.write_bytes(b"pdf")
    drawing = InputRecord(
        "input_drawing_1",
        "drawing",
        "drawing.pdf",
        drawing_path.relative_to(tmp_path).as_posix(),
        drawing_path.stat().st_size,
        "b" * 64,
        "now",
        format_id="drawing",
        representation="document",
    )
    plan = PlanRecord(
        "plan_1",
        "fusion",
        ["step"],
        "ready",
        "now",
        input_ids=[drawing.input_id],
    )

    assert materialize_html_runtime(
        tmp_path, run_id, plan, [drawing], artifacts
    ) is None


def test_runtime_adapter_skips_runs_without_a_pdf_drawing(tmp_path):
    run_id = "run_1"
    artifacts = [
        _artifact(tmp_path, run_id, "dfm_report.json", "report_json", {}),
        _artifact(tmp_path, run_id, "render_scene.json", "render_scene", {}),
        _artifact(
            tmp_path,
            run_id,
            "evidence_geometry.json",
            "evidence_geometry",
            {},
        ),
    ]
    plan = PlanRecord("plan_1", "step", ["step"], "ready", "now")

    assert materialize_html_runtime(tmp_path, run_id, plan, [], artifacts) is None


def test_bundled_html_wrapper_renders_a_self_contained_contract(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    output = tmp_path / "report.html"

    render_html_report(llm_path, runtime_path, output)

    assert output.is_file()
    html = output.read_text(encoding="utf-8")
    assert "evaluation-draft" in html
    assert "three.js" in html.lower()
    assert "综合评估" in html


def test_html_vendor_assets_are_bundled_with_the_reporting_package():
    vendor = (
        Path(__file__).resolve().parents[3]
        / "tools"
        / "dfm"
        / "reporting"
        / "html"
        / "vendor"
    )

    assert (vendor / "three.r128.min.js").is_file()
    assert (vendor / "OrbitControls.r128.js").is_file()


def test_runtime_adapter_output_renders_with_agent_authored_content(tmp_path):
    fixture = tmp_path / "fixture"
    llm_path, original_runtime_path = _html_contract_fixture(fixture)
    original_runtime = json.loads(original_runtime_path.read_text(encoding="utf-8"))
    run_id = original_runtime["report"]["run_id"]
    output_dir = tmp_path / "runs" / run_id / "artifacts"
    output_dir.mkdir(parents=True)
    for name in (
        "dfm_report.json",
        "render_scene.json",
        "scalar_field_wall_thickness.json",
        "scalar_field_draft.json",
        "evidence_geometry.json",
        "evidence_001.png",
    ):
        shutil.copyfile(fixture / name, output_dir / name)

    artifacts = []
    for path in output_dir.iterdir():
        if path.name == "dfm_report.json":
            kind = "report_json"
        elif path.name == "render_scene.json":
            kind = "render_scene"
        elif path.name.startswith("scalar_field_"):
            kind = "scalar_field"
        elif path.name == "evidence_geometry.json":
            kind = "evidence_geometry"
        elif path.name.startswith("evidence_") and path.suffix == ".png":
            kind = "evidence_image"
        else:
            continue
        artifacts.append(
            ArtifactRecord(
                f"artifact_{path.stem}",
                kind,
                path.relative_to(tmp_path).as_posix(),
                "application/json",
                "now",
            )
        )

    drawing_path = tmp_path / "inputs" / "drawing.pdf"
    drawing_path.parent.mkdir(parents=True)
    shutil.copyfile(fixture / "drawing.pdf", drawing_path)
    drawing = InputRecord(
        "input_drawing_1",
        "drawing",
        "drawing.pdf",
        drawing_path.relative_to(tmp_path).as_posix(),
        drawing_path.stat().st_size,
        "b" * 64,
        "now",
        format_id="drawing",
        representation="document",
    )
    plan = PlanRecord(
        "plan_1",
        "fusion",
        ["step"],
        "ready",
        "now",
        process="injection",
        scope_id="injection.default",
        scope_version="1.1.0",
        input_ids=[drawing.input_id],
    )
    observation_artifact = _drawing_observations(
        tmp_path,
        drawing,
        [
            {
                "observation_id": "observation.agent.part-name",
                "input_id": drawing.input_id,
                "kind": "part_name",
                "value": "Housing",
                "source_refs": ["artifact:ocr#fragment=title"],
                "confidence": 0.99,
            }
        ],
    )

    runtime_artifact = materialize_html_runtime(
        tmp_path,
        run_id,
        plan,
        [drawing],
        artifacts,
        semantic_artifacts=[observation_artifact],
    )
    assert runtime_artifact is not None
    html_path = output_dir / "report.html"
    render_html_report(
        llm_path,
        tmp_path / runtime_artifact.relative_path,
        html_path,
    )

    assert html_path.is_file()
    html = html_path.read_text(encoding="utf-8")
    assert "evaluation-draft" in html
    assert "主壁拔模角不足" in html
    assert "three.js" in html.lower()


def test_render_html_action_attaches_agent_content_and_html(
    tmp_path, monkeypatch
):
    from tools.dfm import service as service_module
    from tools.dfm.service import DFMService

    workspace = DFMWorkspace(tmp_path / "workspace")
    manifest = workspace.create_project("HTML report")
    project_dir = workspace.project_dir(manifest.project_id)
    run_id = "run_html"
    output_dir = project_dir / "runs" / run_id / "artifacts"
    output_dir.mkdir(parents=True)
    runtime_path = output_dir / "runtime_data.jsonl"
    runtime_payload = {"schema_version": "dfm-html-runtime/v1"}
    runtime_path.write_text(json.dumps(runtime_payload) + "\n", encoding="utf-8")
    runtime_artifact = ArtifactRecord(
        f"artifact_{run_id}_report_html_runtime",
        "report_html_runtime",
        runtime_path.relative_to(project_dir).as_posix(),
        "application/x-ndjson",
        "now",
    )
    run = RunRecord(
        run_id,
        "step",
        "test",
        RunStatus.REPORTING,
        "now",
        "now",
        artifacts=[runtime_artifact],
        stage="report_editing",
        progress_percent=98,
    )
    service_module.ManifestStore(project_dir).update(
        lambda current: replace(
            current,
            runs=[run],
            artifacts=[runtime_artifact],
        )
    )

    captured = {}

    def fake_render(llm_path, _runtime_path, output_path):
        captured.update(json.loads(llm_path.read_text(encoding="utf-8")))
        output_path.write_text("<html>report</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(service_module, "render_html_report", fake_render)
    llm_content = {
        "schema_version": "dfm-html-llm/v1",
        "part": {
            "name": "housing",
            "general_tolerance": "2 places ±0.100 mm",
            "technical_note": "Edges per ISO 13715.",
        },
        "issues": [],
        "conclusion": {
            "assessment_level": "通过",
            "summary": "No failed checks.",
            "risks": [],
            "actions": [],
        },
    }
    service = DFMService(workspace=workspace, reconcile_jobs=False)
    try:
        context = service.analysis(
            "report_context",
            project_id=manifest.project_id,
            run_id=run_id,
        )
        with pytest.raises(DFMError) as exc_info:
            service.analysis(
                "result",
                project_id=manifest.project_id,
                run_id=run_id,
            )
        result = service.analysis(
            "render_html",
            project_id=manifest.project_id,
            run_id=run_id,
            llm_content=llm_content,
        )
    finally:
        service.close()

    assert captured == llm_content
    assert context["ready"] is True
    assert context["runtime"] == runtime_payload
    assert exc_info.value.code == "result_not_ready"
    assert result["run"]["status"] == "succeeded"
    assert result["run"]["stage"] == "complete"
    assert result["run"]["progress_percent"] == 100
    assert result["report"]["kind"] == "report_html"
    kinds = {item["kind"] for item in result["run"]["artifacts"]}
    assert {"report_html_runtime", "report_html_llm", "report_html"} <= kinds


def test_report_context_returns_pending_without_claiming_success(tmp_path):
    from tools.dfm import service as service_module
    from tools.dfm.service import DFMService

    workspace = DFMWorkspace(tmp_path / "workspace")
    manifest = workspace.create_project("Pending HTML report")
    run = RunRecord(
        "run_pending",
        "step",
        "test",
        RunStatus.RUNNING,
        "now",
        "now",
        stage="rule_evaluation",
        progress_percent=75,
    )
    service_module.ManifestStore(workspace.project_dir(manifest.project_id)).update(
        lambda current: replace(current, runs=[run])
    )
    service = DFMService(workspace=workspace, reconcile_jobs=False)
    try:
        context = service.analysis(
            "report_context",
            project_id=manifest.project_id,
            run_id=run.run_id,
            wait_seconds=0,
        )
    finally:
        service.close()

    assert context["ready"] is False
    assert context["next_action"] == "report_context"
    assert context["run"]["status"] == "running"
