import base64
import gzip
import json
from dataclasses import replace
from pathlib import Path
import re
import shutil
import time

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
from tools.dfm.reporting.html.editor_layout import layout_markup
from tools.dfm.reporting.html.template import generate_html, normalize_contracts


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


def test_html_uses_all_occt_scalar_fields_without_fixed_metric_requirements(tmp_path):
    project_dir = tmp_path / "project"
    llm_path, _ = _html_contract_fixture(project_dir)
    run_id = "run_fixture"
    report = json.loads((project_dir / "dfm_report.json").read_text(encoding="utf-8"))
    report["issues"][0]["code"] = "injection.geometry.main_wall.draft"
    report["issues"][0]["metric"]["operator"] = "between"
    report["issues"][0]["metric"]["expected"] = {"lower": 0.2, "upper": 0.5}
    artifacts = [
        _artifact(project_dir, run_id, "dfm_report.json", "report_json", report),
        _artifact(project_dir, run_id, "render_scene.json", "render_scene", {"schema_version": 2, "primitives": []}),
        _artifact(project_dir, run_id, "evidence_geometry.json", "evidence_geometry", {"failed_patches": []}),
        _artifact(project_dir, run_id, "evidence_001.png", "evidence_image", b"\x89PNG\r\n\x1a\n"),
    ]
    for index, (metric_id, quantity_id) in enumerate((
        ("injection.geometry.main_wall.draft", "inner_draft_angle_deg"),
        ("injection.geometry.main_wall.draft", "inner_draft_angle_deg"),
        ("injection.geometry.screw_boss.wall", "maximum_wall_thickness_mm"),
    ), start=1):
        artifacts.append(_artifact(
            project_dir, run_id, f"scalar_field_{index}.json", "scalar_field",
            {
                "metric_id": metric_id,
                "quantity_id": quantity_id,
                "unit": "degree" if "draft" in metric_id else "mm",
                "samples": [{"sample_id": f"sample-{index}", "value": index}],
                "cells": [{"sample_ids": [f"sample-{index}"], "triangle_ref": {"primitive_id": "face-1", "triangle_id": index}}],
            },
        ))
    plan = PlanRecord("plan_1", "step", ["step"], "ready", "now", process="injection")

    generated = materialize_html_runtime(project_dir, run_id, plan, [], artifacts)
    assert generated is not None
    runtime_path = project_dir / generated.relative_path
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    catalog = runtime["resources"]["field_catalog"]
    assert len(catalog) == 2
    assert {item["metric_id"] for item in catalog} == {
        "injection.geometry.main_wall.draft", "injection.geometry.screw_boss.wall",
    }
    merged = next(item for item in catalog if item["source_count"] == 2)
    merged_payload = json.loads((runtime_path.parent / merged["path"]).read_text(encoding="utf-8"))
    assert len(merged_payload["samples"]) == 2

    html_path = tmp_path / "feature_specific.html"
    generate_html(llm_path, runtime_path, html_path)
    html = html_path.read_text(encoding="utf-8")
    assert "injection.geometry.screw_boss.wall" in html
    assert "const fieldMaps = Object.fromEntries" in html
    assert "0.2–0.5" in html


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


def test_runtime_adapter_allows_runs_without_scalar_fields_or_drawing(tmp_path):
    run_id = "run_1"
    artifacts = [
        _artifact(tmp_path, run_id, "dfm_report.json", "report_json", {"run_id": run_id}),
        _artifact(tmp_path, run_id, "render_scene.json", "render_scene", {}),
    ]
    plan = PlanRecord("plan_1", "step", ["step"], "ready", "now")

    generated = materialize_html_runtime(tmp_path, run_id, plan, [], artifacts)
    assert generated is not None
    runtime = json.loads((tmp_path / generated.relative_path).read_text(encoding="utf-8"))
    assert runtime["resources"]["field_catalog"] == []
    assert json.loads((tmp_path / generated.relative_path).parent.joinpath(
        runtime["resources"]["evidence_geometry_path"]
    ).read_text(encoding="utf-8")) == {"failed_patches": []}


def test_html_renders_without_any_occt_scalar_field(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["resources"]["field_catalog"] = []
    runtime["resources"]["scalar_fields"] = {}
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False) + "\n", encoding="utf-8")

    output = tmp_path / "report_without_fields.html"
    generate_html(llm_path, runtime_path, output)
    html = output.read_text(encoding="utf-8")
    assert "const scalarFields = [];" in html
    assert "evaluation-draft" in html


def test_bundled_html_wrapper_renders_a_self_contained_contract(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    output = tmp_path / "report.html"

    render_html_report(llm_path, runtime_path, output)

    assert output.is_file()
    html = output.read_text(encoding="utf-8")
    assert "evaluation-draft" in html
    source_gzip = re.search(r'"sourceGzip":"([^"]+)"', html)
    assert source_gzip is not None
    source_html = gzip.decompress(base64.b64decode(source_gzip.group(1))).decode(
        "utf-8"
    )
    assert "three.js" in source_html.lower()
    assert "综合评估" in html


def test_html_accepts_plain_text_risks_and_actions(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    llm = json.loads(llm_path.read_text(encoding="utf-8"))
    llm["conclusion"]["risks"] = ["Review the failed draft check."]
    llm["conclusion"]["actions"] = ["Correct the draft angle."]
    llm_path.write_text(json.dumps(llm, ensure_ascii=False) + "\n", encoding="utf-8")

    output = tmp_path / "plain_text_report.html"
    render_html_report(llm_path, runtime_path, output)

    global_insights = normalize_contracts(llm_path, runtime_path)["insights"]["global"]
    assert global_insights["core_risks"] == [
        {"title": "风险 1", "description": "Review the failed draft check."}
    ]
    assert global_insights["optimization_roadmap"] == [
        {"title": "建议 1", "description": "Correct the draft angle."}
    ]
    html = output.read_text(encoding="utf-8")
    source_gzip = re.search(r'"sourceGzip":"([^"]+)"', html)
    assert source_gzip is not None
    source_html = gzip.decompress(base64.b64decode(source_gzip.group(1))).decode("utf-8")
    assert "evaluation-draft" in source_html


def test_html_summary_counts_failed_checks_by_issue_type_without_severity(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    llm = json.loads(llm_path.read_text(encoding="utf-8"))
    first = runtime["report"]["issues"][0]
    first["check_id"] = "check.main_wall_minimum_draft"
    first["severity"] = "warning"
    second = {
        **first,
        "id": "evaluation-wall",
        "check_id": "check.main_wall_minimum_thickness",
        "code": "injection.geometry.wall_thickness",
        "severity": "critical",
    }
    runtime["report"]["issues"].append(second)
    runtime["report"]["stats"]["failed_count"] = 2
    llm["issues"].append({
        "issue_id": "evaluation-wall",
        "title": "Wall thickness check",
        "description": "The measured wall thickness does not meet the rule.",
    })
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False) + "\n", encoding="utf-8")
    llm_path.write_text(json.dumps(llm, ensure_ascii=False) + "\n", encoding="utf-8")

    output = tmp_path / "report.html"
    generate_html(llm_path, runtime_path, output)
    html = output.read_text(encoding="utf-8")
    summary = html.split('<div class="summary-stat-strip"', 1)[1].split(
        '<div class="webgl-container"', 1
    )[0]
    cards = re.findall(
        r'<div class="stat-value" data-target="(\d+)">\d+</div><div class="stat-label">([^<]+)</div>',
        summary,
    )
    by_label = {label: int(value) for value, label in cards}
    assert by_label == {
        "问题总数": 2,
        "拔模角问题": 1,
        "壁厚问题": 1,
    }
    assert "severity-badge" not in html


def test_html_summary_embeds_failed_patch_locations_and_cell_based_scalar_fields(tmp_path):
    fixture = tmp_path / "fixture"
    llm_path, runtime_path = _html_contract_fixture(fixture)
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["report"]["issues"][0]["images"] = []
    runtime["report"]["issues"][0]["image"] = None
    runtime["global_issue_metadata"] = [{
        "issue_id": "evaluation-draft",
        "source_issue_id": "evaluation-draft",
    }]
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False) + "\n", encoding="utf-8")
    (fixture / "evidence_geometry.json").write_text(json.dumps({
        "failed_patches": [{
            "evaluation_id": "evaluation-draft",
            "triangle_refs": [{
                "primitive_id": "face-9",
                "triangle_id": 1,
                "render_mesh_snapshot_id": "mesh-current",
            }],
        }],
    }), encoding="utf-8")
    (fixture / "scalar_field_draft.json").write_text(json.dumps({
        "metric_id": "injection.geometry.draft",
        "samples": [{"sample_id": "scalar-field-sample-1", "value": 0.5}],
        "cells": [{
            "triangle_ref": {"primitive_id": "face-9", "triangle_id": 1},
            "sample_ids": ["scalar-field-sample-1"],
        }],
    }), encoding="utf-8")

    output = tmp_path / "report.html"
    generate_html(llm_path, runtime_path, output)

    html = output.read_text(encoding="utf-8")
    assert 'data-mode="issues"' in html
    assert 'data-issue-nav="evaluation-draft"' in html
    assert '"triangle_refs": [{"primitive_id": "face-9", "triangle_id": 1' in html
    assert 'const fieldMaps = Object.fromEntries(scalarFields.map(field => [field.id, fieldValuesByTriangle(field)]));' in html
    assert 'const failed = failedTriangleKeys(mode);' in html
    assert 'sample_id.match(/face-' not in html
    assert "mode === 'thickness' ? 1.2 : 1.0" not in html


def test_html_shows_every_failed_check_with_evaluated_units_not_llm_verdict(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    llm = json.loads(llm_path.read_text(encoding="utf-8"))
    ratio_issue = runtime["report"]["issues"][0]
    ratio_issue["id"] = "evaluation-ratio"
    ratio_issue["check_id"] = "C_SCREW_BOSS_HOLE_DEPTH"
    ratio_issue["code"] = "injection.geometry.screw_boss.hole"
    ratio_issue["title"] = "螺钉柱－孔深"
    ratio_issue["recommendation"] = "控制螺钉柱孔深与孔径的比例。"
    ratio_issue["images"] = []
    ratio_issue["image"] = None
    ratio_issue["metric"].update({
        "actual": 4.45,
        "expected": 2,
        "operator": "<=",
        "actual_unit": "ratio",
        "measurement_ids": ["measurement-hole_diameter_mm-region-1"],
    })
    runtime["global_issue_metadata"] = [{
        "issue_id": "evaluation-ratio",
        "source_issue_id": "evaluation-ratio",
    }]
    draft_issue = {
        **ratio_issue,
        "id": "evaluation-draft",
        "check_id": "C_WALL_DRAFT",
        "code": "injection.geometry.main_wall.draft",
        "title": "主体壁－拔模角",
        "recommendation": "增加主体壁拔模角。",
        "images": ["evidence_001.png"],
        "image": "evidence_001.png",
        "metric": {**ratio_issue["metric"], "actual": -3, "expected": 2, "operator": ">=", "actual_unit": "degree"},
    }
    runtime["report"]["issues"].append(draft_issue)
    runtime["report"]["stats"]["failed_count"] = 2
    llm["issues"] = [
        {"issue_id": "evaluation-ratio", "title": "完全合格", "description": "不构成实际风险。"},
        {"issue_id": "evaluation-draft", "title": "没有拔模问题", "description": "可以直接脱模。"},
    ]
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False) + "\n", encoding="utf-8")
    llm_path.write_text(json.dumps(llm, ensure_ascii=False) + "\n", encoding="utf-8")

    output = tmp_path / "report.html"
    generate_html(llm_path, runtime_path, output)
    html = output.read_text(encoding="utf-8")
    assert html.count('class="slide finding-slide"') == 2
    assert 'data-issue-id="evaluation-ratio"' in html
    assert 'data-issue-id="evaluation-draft"' in html
    assert 'data-issue-nav="evaluation-ratio"' in html
    assert 'data-issue-nav="evaluation-draft"' in html
    assert "规则判定未通过：实测 4.45 倍；规则要求 ≤ 2 倍。" in html
    assert "规则判定未通过：实测 -3 °；规则要求 ≥ 2 °。" in html
    assert "控制螺钉柱孔深与孔径的比例。" in html
    assert "增加主体壁拔模角。" in html
    assert "本项未生成截图证据" in html
    assert "不构成实际风险" not in html
    assert "可以直接脱模" not in html


def test_html_preserves_small_nonzero_measured_values(tmp_path):
    llm_path, runtime_path = _html_contract_fixture(tmp_path / "fixture")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["report"]["issues"][0]["metric"].update({
        "actual": 1.08924689359538e-06,
        "actual_unit": "mm",
    })
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False) + "\n", encoding="utf-8")

    output = tmp_path / "report.html"
    generate_html(llm_path, runtime_path, output)
    html = output.read_text(encoding="utf-8")
    assert "实测 1.09e-06 mm" in html
    assert ">1.09e-06</span>" in html


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
    assert "规则判定未通过" in html
    source_gzip = re.search(r'"sourceGzip":"([^"]+)"', html)
    assert source_gzip is not None
    source_html = gzip.decompress(base64.b64decode(source_gzip.group(1))).decode(
        "utf-8"
    )
    assert "three.js" in source_html.lower()


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
    runtime_payload = {
        "schema_version": "dfm-html-runtime/v1",
        "report": {"issues": [{"id": "issue-real"}]},
    }
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
        plan_snapshot={"large_plan": "x" * 150_000},
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
    progress_events = []

    def fake_render(llm_path, _runtime_path, output_path, *, on_progress=None):
        captured.update(json.loads(llm_path.read_text(encoding="utf-8")))
        if on_progress is not None:
            on_progress("report_layout")
        output_path.write_text("<html>report</html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(service_module, "render_html_report", fake_render)
    def capture_progress(event_type, name, preview, _args, **details):
        progress_events.append((event_type, name, preview, details))

    llm_content = {
        "schema_version": "dfm-html-llm/v1",
        "part": {
            "name": "housing",
            "general_tolerance": "2 places ±0.100 mm",
            "technical_note": "Edges per ISO 13715.",
        },
        "issues": [
            {"issue_id": "issue-real", "title": "Wall issue", "description": "Check wall."}
        ],
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
        with pytest.raises(DFMError) as invalid_content:
            service.analysis(
                "render_html",
                project_id=manifest.project_id,
                run_id=run_id,
                llm_content={
                    **llm_content,
                    "issues": [
                        {"issue_id": "issue-guessed", "title": "Wrong", "description": "Wrong."}
                    ],
                },
            )
        assert service.jobs.status(manifest.project_id, run_id).status is RunStatus.REPORTING
        with pytest.raises(DFMError) as malformed_content:
            service.analysis(
                "render_html",
                project_id=manifest.project_id,
                run_id=run_id,
                llm_content={
                    **llm_content,
                    "conclusion": {**llm_content["conclusion"], "risks": [None]},
                },
            )
        assert malformed_content.value.code == "report_content_invalid"
        assert service.jobs.status(manifest.project_id, run_id).status is RunStatus.REPORTING
        plain_content = {
            **llm_content,
            "conclusion": {
                **llm_content["conclusion"],
                "risks": ["Review the wall issue."],
                "actions": ["Correct the wall."],
            },
        }
        result = service.analysis(
            "render_html",
            project_id=manifest.project_id,
            run_id=run_id,
            llm_content=plain_content,
            _tool_progress_callback=capture_progress,
            _tool_call_id="tool-render-html",
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = service.analysis(
                "status",
                project_id=manifest.project_id,
                run_id=run_id,
            )
            if status["run"]["status"] == "succeeded":
                break
            time.sleep(0.01)
        finished = service.analysis(
            "result",
            project_id=manifest.project_id,
            run_id=run_id,
        )
    finally:
        service.close()

    assert captured["issues"] == llm_content["issues"]
    assert captured["conclusion"]["risks"] == [
        {"title": "风险 1", "description": "Review the wall issue."}
    ]
    assert captured["conclusion"]["actions"] == [
        {"title": "建议 1", "description": "Correct the wall."}
    ]
    assert context["ready"] is True
    assert context["runtime"] == runtime_payload
    assert context["required_issue_ids"] == ["issue-real"]
    assert "plan_snapshot" not in context["run"]
    assert len(json.dumps(context)) < 100_000
    assert exc_info.value.code == "result_not_ready"
    assert invalid_content.value.code == "report_content_invalid"
    assert invalid_content.value.details == {
        "required_issue_ids": ["issue-real"],
        "missing_issue_ids": ["issue-real"],
        "unknown_issue_ids": ["issue-guessed"],
    }
    assert result["accepted"] is True
    assert result["complete"] is False
    assert result["next_action"] == "status"
    assert result["run"]["status"] == "reporting"
    assert finished["run"]["status"] == "succeeded"
    assert finished["run"]["stage"] == "complete"
    assert finished["run"]["progress_percent"] == 100
    kinds = {item["kind"] for item in finished["run"]["artifacts"]}
    assert {"report_html_runtime", "report_html_llm", "report_html"} <= kinds
    assert any(event[3]["stage"] == "report_layout" for event in progress_events)
    assert progress_events[-1][0] == "background.tool.complete"
    assert progress_events[-1][3]["status"] == "succeeded"
    assert progress_events[-1][3]["report_html"].endswith("report.html")


def test_layout_markup_skips_embedded_geometry_scripts_but_keeps_slide_dom():
    large_scene = "mesh-coordinate," * 10000
    markup = (
        "<html><head><style>.slide{width:10px}</style>"
        f"<script>const sceneData='{large_scene}'</script></head>"
        "<body><section class='slide'>report</section>"
        "<script src='three.js'></script></body></html>"
    )

    prepared = layout_markup(markup)

    assert "mesh-coordinate" not in prepared
    assert "<script" not in prepared.lower()
    assert ".slide{width:10px}" in prepared
    assert "<section class='slide'>report</section>" in prepared


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


def test_report_context_returns_terminal_failure_as_status(tmp_path):
    from tools.dfm import service as service_module
    from tools.dfm.service import DFMService

    workspace = DFMWorkspace(tmp_path / "workspace")
    manifest = workspace.create_project("Failed DFM run")
    run = RunRecord(
        "run_failed",
        "step",
        "test",
        RunStatus.FAILED,
        "now",
        "now",
        stage="geometry_analysis",
        progress_percent=40,
        error={"code": "pull_direction_invalid", "message": "Invalid direction."},
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

    assert context["ok"] is True
    assert context["ready"] is False
    assert context["complete"] is True
    assert context["next_action"] == "status"
    assert context["run"]["status"] == "failed"
