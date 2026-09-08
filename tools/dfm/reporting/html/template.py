

import argparse
import base64
import json
import os
import re
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_VENDOR_DIR = SCRIPT_DIR / "vendor"


class ContractError(ValueError):
    """Raised when either HTML input contract is incomplete or invalid."""


def load_single_jsonl(path, label):
    """Load the contract's single JSON object from a JSONL file."""
    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ContractError(f"{label} must contain exactly one non-empty JSONL record.")
    value = json.loads(lines[0])
    if not isinstance(value, dict):
        raise ContractError(f"{label} must contain one JSON object.")
    return value


def require_object(parent, key, location):
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"{location}.{key} must be an object.")
    return value


def require_list(parent, key, location):
    value = parent.get(key)
    if not isinstance(value, list):
        raise ContractError(f"{location}.{key} must be an array.")
    return value


def require_text(parent, key, location):
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{location}.{key} must be a non-empty string.")
    return value


def require_nullable_text(parent, key, location):
    if key not in parent:
        raise ContractError(f"{location}.{key} is required.")
    value = parent[key]
    if value is not None and not isinstance(value, str):
        raise ContractError(f"{location}.{key} must be a string or null.")
    return value


def resolve_file(raw_path, base_dir, label):
    path = Path(raw_path)
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise ContractError(f"{label} does not exist: {path}")
    return path


def resolve_directory(raw_path, base_dir, label):
    path = Path(raw_path)
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.is_dir():
        raise ContractError(f"{label} does not exist: {path}")
    return path


def normalize_contracts(llm_path, runtime_path):
    """Validate both schemas and expose the variables consumed by the V6 template."""
    llm = load_single_jsonl(llm_path, "LLM JSONL")
    runtime = load_single_jsonl(runtime_path, "runtime JSONL")
    if llm.get("schema_version") != "dfm-html-llm/v1":
        raise ContractError("llm.schema_version must be dfm-html-llm/v1.")
    if runtime.get("schema_version") != "dfm-html-runtime/v1":
        raise ContractError("runtime.schema_version must be dfm-html-runtime/v1.")

    part = require_object(llm, "part", "llm")
    require_text(part, "name", "llm.part")
    require_nullable_text(part, "general_tolerance", "llm.part")
    require_nullable_text(part, "technical_note", "llm.part")
    conclusion = require_object(llm, "conclusion", "llm")
    require_text(conclusion, "assessment_level", "llm.conclusion")
    require_text(conclusion, "summary", "llm.conclusion")
    for section in ("risks", "actions"):
        for index, item in enumerate(require_list(conclusion, section, "llm.conclusion")):
            location = f"llm.conclusion.{section}[{index}]"
            if not isinstance(item, dict):
                raise ContractError(f"{location} must be an object.")
            require_text(item, "title", location)
            require_text(item, "description", location)
    issues_copy = require_list(llm, "issues", "llm")

    display = require_object(runtime, "display", "runtime")
    require_text(display, "process", "runtime.display")
    require_text(display, "rule_scope", "runtime.display")
    report = require_object(runtime, "report", "runtime")
    report_issues = require_list(report, "issues", "runtime.report")
    global_issue_metadata = require_list(runtime, "global_issue_metadata", "runtime")
    for key in (
        "run_id",
        "input_sha256",
        "process",
        "scope_id",
        "scope_version",
        "producer",
        "producer_contract",
    ):
        require_text(report, key, "runtime.report")
    if "schema_version" not in report:
        raise ContractError("runtime.report.schema_version is required.")
    stats = require_object(report, "stats", "runtime.report")
    for key in ("measurement_count", "evaluation_count", "failed_count"):
        value = stats.get(key)
        if not isinstance(value, int) or value < 0:
            raise ContractError(f"runtime.report.stats.{key} must be a non-negative integer.")
    model_metrics = require_list(runtime, "model_metrics", "runtime")
    resources = require_object(runtime, "resources", "runtime")
    scalar_fields = require_object(resources, "scalar_fields", "runtime.resources")
    base_dir = Path(runtime_path).resolve().parent
    resolved = {
        "evidence_root": resolve_directory(
            require_text(resources, "evidence_root", "runtime.resources"),
            base_dir,
            "runtime.resources.evidence_root",
        ),
        "scene_path": resolve_file(
            require_text(resources, "scene_path", "runtime.resources"),
            base_dir,
            "runtime.resources.scene_path",
        ),
        "thickness_path": resolve_file(
            require_text(scalar_fields, "thickness", "runtime.resources.scalar_fields"),
            base_dir,
            "runtime.resources.scalar_fields.thickness",
        ),
        "draft_path": resolve_file(
            require_text(scalar_fields, "draft", "runtime.resources.scalar_fields"),
            base_dir,
            "runtime.resources.scalar_fields.draft",
        ),
        "evidence_geometry_path": resolve_file(
            require_text(resources, "evidence_geometry_path", "runtime.resources"),
            base_dir,
            "runtime.resources.evidence_geometry_path",
        ),
        "drawing_pdf_path": resolve_file(
            require_text(resources, "drawing_pdf_path", "runtime.resources"),
            base_dir,
            "runtime.resources.drawing_pdf_path",
        ),
        "rule_library_path": resolve_file(
            require_text(resources, "rule_library_path", "runtime.resources"),
            base_dir,
            "runtime.resources.rule_library_path",
        ),
    }

    copy_by_id = {}
    for index, item in enumerate(issues_copy):
        if not isinstance(item, dict):
            raise ContractError(f"llm.issues[{index}] must be an object.")
        issue_id = require_text(item, "issue_id", f"llm.issues[{index}]")
        require_text(item, "title", f"llm.issues[{index}]")
        require_text(item, "description", f"llm.issues[{index}]")
        if issue_id in copy_by_id:
            raise ContractError(f"Duplicate llm issue_id: {issue_id}")
        copy_by_id[issue_id] = {
            "human_title": item["title"],
            "translated_message": item["description"],
        }

    for index, issue in enumerate(report_issues):
        location = f"runtime.report.issues[{index}]"
        if not isinstance(issue, dict):
            raise ContractError(f"{location} must be an object.")
        require_text(issue, "id", location)
        require_text(issue, "code", location)
        require_text(issue, "severity", location)
        images = require_list(issue, "images", location)
        for image_index, image_name in enumerate(images):
            if not isinstance(image_name, str) or not image_name:
                raise ContractError(f"{location}.images[{image_index}] must be a non-empty string.")
        metric = issue.get("metric")
        if metric is not None:
            if not isinstance(metric, dict):
                raise ContractError(f"{location}.metric must be an object or null.")
            for key in (
                "actual",
                "expected",
                "operator",
                "rule_id",
                "rule_version",
                "rule_hash",
                "measurement_ids",
                "backend",
                "certified",
                "algorithm_version",
            ):
                if key not in metric:
                    raise ContractError(f"{location}.metric.{key} is required.")
            if metric["operator"] not in {">=", "<=", ">", "<", "=="}:
                raise ContractError(f"{location}.metric.operator is invalid.")
            if not isinstance(metric["measurement_ids"], list):
                raise ContractError(f"{location}.metric.measurement_ids must be an array.")
            if not isinstance(metric["certified"], bool):
                raise ContractError(f"{location}.metric.certified must be a boolean.")

    for index, item in enumerate(model_metrics):
        location = f"runtime.model_metrics[{index}]"
        if not isinstance(item, dict):
            raise ContractError(f"{location} must be an object.")
        require_text(item, "label", location)
        if "value" not in item or not isinstance(item["value"], (str, int, float)):
            raise ContractError(f"{location}.value must be a string or number.")

    for issue in report_issues:
        for image_name in issue["images"]:
            if not (resolved["evidence_root"] / image_name).is_file():
                raise ContractError(f"Evidence image does not exist: {image_name}")

    runtime_by_id = {
        str(item.get("id")): item
        for item in report_issues
        if isinstance(item, dict) and item.get("id")
    }
    missing_copy = sorted(set(runtime_by_id) - set(copy_by_id))
    orphan_copy = sorted(set(copy_by_id) - set(runtime_by_id))
    if missing_copy or orphan_copy:
        details = []
        if missing_copy:
            details.append("missing LLM issues: " + ", ".join(missing_copy))
        if orphan_copy:
            details.append("orphan LLM issues: " + ", ".join(orphan_copy))
        raise ContractError("Issue IDs must match across both contracts; " + "; ".join(details))

    global_metadata_by_id = {}
    for index, item in enumerate(global_issue_metadata):
        location = f"runtime.global_issue_metadata[{index}]"
        if not isinstance(item, dict):
            raise ContractError(f"{location} must be an object.")
        issue_id = require_text(item, "issue_id", location)
        require_text(item, "source_issue_id", location)
        require_text(item, "severity", location)
        global_metadata_by_id[issue_id] = item

    no_evidence_ids = {
        issue_id
        for issue_id, issue in runtime_by_id.items()
        if not issue.get("images") and not issue.get("image")
    }
    if set(global_metadata_by_id) != no_evidence_ids:
        raise ContractError(
            "runtime.global_issue_metadata must contain exactly the issues without evidence."
        )

    global_issues = []
    for issue_id, issue in runtime_by_id.items():
        if issue.get("images") or issue.get("image"):
            continue
        copy = copy_by_id[issue_id]
        metadata = global_metadata_by_id[issue_id]
        global_issues.append(
            {
                "original_id": metadata["source_issue_id"],
                "title": copy["human_title"],
                "severity": metadata["severity"],
                "description": copy["translated_message"],
            }
        )

    insights = {
        "global": {
            "assessment_level": conclusion["assessment_level"],
            "summary_paragraph": conclusion["summary"],
            "core_risks": require_list(conclusion, "risks", "llm.conclusion"),
            "optimization_roadmap": require_list(conclusion, "actions", "llm.conclusion"),
        },
        "issues": {
            issue_id: {
                "human_title": copy["human_title"],
                "translated_message": copy["translated_message"],
            }
            for issue_id, copy in copy_by_id.items()
        },
        "model_metrics": model_metrics,
        "cover_info": display,
        "global_issues": global_issues,
        "metadata_translations": {
            "tolerance": part.get("general_tolerance"),
            "note": part.get("technical_note"),
        },
    }
    return {
        "part": part,
        "data": report,
        "insights": insights,
        "resources": resolved,
    }


def load_vendor_script(filename, vendor_dir):
    path = Path(vendor_dir) / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Offline runtime is missing: {path}. "
            "The leadership demo must be generated with its pinned vendor assets."
        )
    return path.read_text(encoding="utf-8")


def get_base64_image(image_filename, evidence_root):
    if not image_filename:
        return ""
    image_path = Path(evidence_root) / image_filename
    if not image_path.is_file():
        return ""
    with image_path.open("rb") as file:
        return f"data:image/png;base64,{base64.b64encode(file.read()).decode('utf-8')}"

def inch2px(inches):
    return inches * 96

def compact_number(value, digits=3):
    if not isinstance(value, (int, float)):
        return value
    text = f"{value:.{digits}f}".rstrip('0').rstrip('.')
    return text if text else "0"

def generate_html(llm_jsonl_path, runtime_jsonl_path, output_html_path, vendor_dir=DEFAULT_VENDOR_DIR):
    contract = normalize_contracts(llm_jsonl_path, runtime_jsonl_path)
    part = contract["part"]
    data = contract["data"]
    insights = contract["insights"]
    resources = contract["resources"]
    evidence_root = resources["evidence_root"]

    # Pin and inline the exact runtime used by the existing demo. The delivered
    # HTML is therefore a true single-file artifact and never needs a CDN.
    three_js = load_vendor_script("three.r128.min.js", vendor_dir)
    orbit_controls_js = load_vendor_script("OrbitControls.r128.js", vendor_dir)

    scope_id = insights.get("cover_info", {}).get("rule_scope", "injection.default")
    process = insights.get("cover_info", {}).get("process", "injection")
    metadata = data.get('metadata', {})
    stats = data.get('stats', {})
    issues = [i for i in data.get('issues', []) if isinstance(i, dict)]

    geometry_path = resources["evidence_geometry_path"]
    geometry_data = []
    if os.path.exists(geometry_path):
        with open(geometry_path, 'r', encoding='utf-8') as f:
            geometry_raw = json.load(f)
            geometry_data = geometry_raw.get("failed_patches", []) if isinstance(geometry_raw, dict) else geometry_raw

    # Build raw trace map
    raw_trace_map = {}
    for issue in issues:
        eval_id = issue.get("id")
        if eval_id:
            raw_trace_map[eval_id] = {
                "rule_evaluation": issue,
                "geometry_patches": [p for p in geometry_data if p.get("evaluation_id") == eval_id]
            }

    scene_path = resources["scene_path"]
    thickness_path = resources["thickness_path"]
    draft_path = resources["draft_path"]
    obs_part_name = part.get("name")
    obs_tolerance = part.get("general_tolerance")
    obs_note = part.get("technical_note")

    scene_data_str = "{}"
    thickness_data_str = "{}"
    draft_data_str = "{}"

    with open(scene_path, 'r', encoding='utf-8') as f:
        scene_data_str = f.read()
    with open(thickness_path, 'r', encoding='utf-8') as f:
        thickness_data_str = f.read()
    with open(draft_path, 'r', encoding='utf-8') as f:
        draft_data_str = f.read()

    # Fetch traceability info
    ontology_path = resources["rule_library_path"]
    ontology_b64 = ""
    if os.path.exists(ontology_path):
        with open(ontology_path, "rb") as f:
            ontology_b64 = base64.b64encode(f.read()).decode('utf-8')

    gallery_data = {}



    # Split issues based on images
    issues_with_evidence = []
    issues_without_evidence = []
    for issue in issues:
        raw_images = issue.get("images")
        values = list(raw_images) if isinstance(raw_images, list) else []
        if issue.get("image") and issue.get("image") not in values:
            values.append(issue.get("image"))

        has_valid_img = False
        for img_name in values:
            if img_name and (evidence_root / img_name).is_file():
                has_valid_img = True
                break

        if has_valid_img:
            issues_with_evidence.append(issue)
        else:
            issues_without_evidence.append(issue)

    # Compact UI index used by issue navigation and the readable trace drawer.
    issue_ui_data = {}
    for issue in issues_with_evidence:
        issue_id = str(issue.get("id") or "DFM")
        code = str(issue.get("code") or "")
        metric = issue.get("metric") if isinstance(issue.get("metric"), dict) else {}
        measurement_ids = metric.get("measurement_ids") if isinstance(metric.get("measurement_ids"), list) else []
        unit = ""
        if measurement_ids:
            if "_mm-" in measurement_ids[0]: unit = "mm"
            elif "_deg-" in measurement_ids[0]: unit = "°"
        image_names = list(issue.get("images") or []) if isinstance(issue.get("images"), list) else []
        if issue.get("image") and issue.get("image") not in image_names:
            image_names.append(issue.get("image"))
        issue_insight = insights.get("issues", {}).get(issue_id, {})
        issue_ui_data[issue_id] = {
            "id": issue_id,
            "title": issue_insight.get("human_title") or issue.get("title") or code or "DFM 问题",
            "code": code,
            "mode": "thickness" if "thickness" in code.lower() else "draft",
            "severity": str(issue.get("severity") or "unclassified").lower(),
            "actual": compact_number(metric.get("actual", "N/A")),
            "expected": compact_number(metric.get("expected", "N/A")),
            "operator": metric.get("operator", ""),
            "unit": unit,
            "rule_id": metric.get("rule_id", ""),
            "rule_version": metric.get("rule_version", ""),
            "rule_hash": metric.get("rule_hash", ""),
            "measurement_ids": measurement_ids,
            "backend": metric.get("backend", ""),
            "algorithm_version": metric.get("algorithm_version", ""),
            "certified": metric.get("certified"),
            "evidence": image_names,
            "input_sha256": data.get("input_sha256", ""),
        }

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>DFM Analysis Report · Interactive V5</title>
    <style>
        :root {{
            color-scheme: light;
            --canvas: #101B2A;
            --canvas-deep: #070D16;
            --paper: #F6F8FA;
            --surface: #FFFFFF;
            --surface-soft: #F1F5F7;
            --ink: #16212B;
            --muted: #667481;
            --line: #DDE4E9;
            --brand: #087E8B;
            --brand-hover: #056A75;
            --brand-soft: #E5F4F4;
            --risk: #D92D20;
            --radius-sm: 6px;
            --radius-md: 9px;
            --radius-lg: 14px;
            --shadow-panel: 0 10px 26px rgba(16,33,43,.075);
            --shadow-page: 0 26px 64px rgba(0,0,0,.32), 0 2px 8px rgba(0,0,0,.22);
        }}
        * {{ box-sizing: border-box; }}
        button:focus-visible, [role="button"]:focus-visible, summary:focus-visible {{
            outline: 3px solid rgba(8,126,139,.38); outline-offset: 2px;
        }}
        html {{ scroll-behavior: smooth; background: var(--canvas-deep); }}
        body {{
            min-height: 100vh; margin: 0; padding: 44px 24px 72px;
            display: flex; flex-direction: column; align-items: center; gap: 34px;
            font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
            color: var(--ink);
            background:
                linear-gradient(rgba(124,158,174,.032) 1px, transparent 1px),
                linear-gradient(90deg, rgba(124,158,174,.032) 1px, transparent 1px),
                radial-gradient(circle at 18% 8%, rgba(8,126,139,.18), transparent 32rem),
                radial-gradient(circle at 84% 42%, rgba(41,98,255,.09), transparent 36rem),
                linear-gradient(145deg, var(--canvas) 0%, var(--canvas-deep) 100%);
            background-size: 48px 48px, 48px 48px, auto, auto, auto;
            background-attachment: fixed;
        }}
        .slide {{
            width: {inch2px(13.333)}px; height: {inch2px(7.5)}px;
            background-color: var(--paper); position: relative;
            border: 1px solid rgba(255,255,255,.14); border-radius: var(--radius-lg);
            box-shadow: var(--shadow-page);
            overflow: hidden; box-sizing: border-box; isolation: isolate;
            transform: translateZ(0);
        }}
        .slide::after {{
            content: ""; position: absolute; inset: 0; z-index: 500; pointer-events: none;
            border-radius: inherit; box-shadow: inset 0 0 0 1px rgba(255,255,255,.18);
        }}
        .slide:not(.cover-slide) {{
            background: linear-gradient(180deg, #FBFCFD 0%, var(--paper) 100%);
        }}
        .slide:not(.cover-slide)::before {{
            content:""; position:absolute; z-index:2; left:0; right:0; top:0; height:3px;
            background:linear-gradient(90deg, var(--brand) 0 168px, rgba(8,126,139,.18) 168px, transparent 54%);
            pointer-events:none;
        }}
        .cover-slide::before {{
            content: ""; position: absolute; inset: 0; z-index: 1; pointer-events: none;
            background:
                radial-gradient(circle at 76% 45%, rgba(8,126,139,.20), transparent 30%),
                linear-gradient(120deg, rgba(255,255,255,.035), transparent 42%);
        }}
        .element {{ position: absolute; box-sizing: border-box; }}
        .text-box {{
            display: flex; flex-direction: column;
            padding-left: {inch2px(0.04)}px; padding-right: {inch2px(0.04)}px;
            padding-top: {inch2px(0.02)}px; padding-bottom: {inch2px(0.02)}px;
            word-wrap: break-word; word-break: break-word; white-space: pre-wrap;
            overflow: visible; height: auto !important;
            outline: none; border: 1px solid transparent; border-radius: 4px;
            transition: border-color .16s ease, background .16s ease, box-shadow .16s ease;
            text-rendering: optimizeLegibility; -webkit-font-smoothing: antialiased;
        }}
        .text-box:hover {{ box-shadow:inset 0 0 0 1px rgba(8,126,139,.18); background-color:rgba(255,255,255,.32); cursor:text; }}
        .text-box:focus {{ border-color:rgba(8,126,139,.56); background-color:rgba(255,255,255,.82); box-shadow:0 0 0 3px rgba(8,126,139,.12); cursor:text; }}
        .cover-slide .text-box:hover {{ background:rgba(255,255,255,.055); box-shadow:inset 0 0 0 1px rgba(184,221,225,.18); }}
        .cover-slide .text-box:focus {{ color:#FFFFFF !important; background:rgba(15,23,42,.58); }}
        .text-box::selection {{ background: rgba(8,126,139,.20); }}
        .shape {{
            border-radius: var(--radius-md);
            box-shadow: 0 1px 2px rgba(16,24,40,.04), inset 0 0 0 1px rgba(16,24,40,.035);
        }}
        .shape-rect {{ border-radius: 0; }}
        .picture-frame {{
            border-radius: var(--radius-md); overflow: hidden; padding: 6px;
            background: linear-gradient(180deg, #FFFFFF 0%, #F8FAFC 100%);
            box-shadow: inset 0 0 0 1px var(--line), 0 4px 12px rgba(16,33,43,.035);
            transition: transform .24s cubic-bezier(.22,.8,.25,1), box-shadow .24s ease;
        }}
        .finding-slide .picture-frame:hover {{
            transform: translateY(-2px);
            box-shadow: inset 0 0 0 1px #BFCED5, 0 13px 28px rgba(16,33,43,.11);
        }}
        .img-contain {{ width: 100%; height: 100%; object-fit: contain; pointer-events: none; border-radius: 5px; }}

        /* 3D Container Styles */
        .webgl-wrapper {{
            position: absolute; border-radius: 11px; overflow: hidden;
            border: 1px solid rgba(255,255,255,.14);
            box-shadow: inset 0 0 36px rgba(0,0,0,.17), 0 12px 28px rgba(15,23,42,.16);
            background:
                radial-gradient(circle at 50% 46%, rgba(85,111,132,.58) 0%, rgba(37,48,62,.76) 32%, transparent 64%),
                linear-gradient(160deg, #25303D 0%, #111823 72%, #0A0F17 100%);
        }}
        .webgl-wrapper::after {{
            content: ""; position: absolute; inset: 0; z-index: 0; pointer-events: none;
            background: radial-gradient(ellipse at 50% 78%, rgba(0,0,0,.32), transparent 42%);
        }}
        .webgl-container {{
            position: relative; z-index: 1; width: 100%; height: 100%; cursor: grab;
            background-color:transparent; background-position:center; background-repeat:no-repeat; background-size:contain;
        }}
        .webgl-container.webgl-live {{ background-image:none !important; }}
        .webgl-container:active {{ cursor: grabbing; }}

        /* Glassmorphism Control Panel */
        .glass-panel {{
            position: absolute; top: 15px; left: 15px; z-index: 10;
            background: rgba(15, 23, 42, .58);
            backdrop-filter: blur(14px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 10px;
            padding: 5px;
            display: flex; gap: 5px;
            box-shadow: 0 7px 18px rgba(0,0,0,.24);
        }}
        .glass-btn {{
            background: transparent; border: none; color: #fff;
            padding: 8px 16px; border-radius: 6px; font-size: 12px;
            font-weight: bold; cursor: pointer; transition: all 0.2s ease;
        }}
        .glass-btn.active {{
            background: rgba(8,126,139,.90);
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .glass-btn:hover:not(.active) {{
            background: rgba(255, 255, 255, 0.15);
        }}

        /* Legend */
        .legend {{
            position: absolute; bottom: 15px; left: 15px; z-index: 10;
            display: flex; flex-direction: column; gap: 8px; pointer-events: none;
            padding: 9px 11px; border-radius: 8px;
            background: rgba(15,23,42,.58); backdrop-filter: blur(10px);
        }}
        .legend-item {{ display: flex; align-items: center; gap: 6px; }}
        .legend-color {{ width: 12px; height: 12px; border-radius: 3px; }}
        .legend-text {{ color: #F2F4F7; font-size: 10px; }}

        /* Action Button */
        .action-btn {{
            display: flex; align-items: center; justify-content: center;
            background: linear-gradient(135deg, #087E8B 0%, #055A63 100%);
            color: white; text-decoration: none; font-size: 14pt; font-weight: bold;
            border-radius: var(--radius-md); box-shadow: 0 5px 14px rgba(8,126,139,.24);
            transition: transform .18s ease, box-shadow .18s ease, background .18s ease; cursor: pointer;
        }}
        .action-btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 9px 20px rgba(8,126,139,.34);
        }}
        .minimap {{ border-radius: 50%; background-color: #14213D; }}
        .hover-row {{ border: 2px solid transparent; transition: all 0.2s; }}
        .hover-row:hover {{ border: 2px solid #087E8B; box-shadow: 0 4px 10px rgba(8,126,139,0.3); }}
        .rules-tooltip:hover .rules-content {{ display: block !important; }}
        .section-eyebrow {{
            color: #087E8B; font-size: 10px; font-weight: 800;
            letter-spacing: .15em; text-transform: uppercase;
        }}
        .page-ghost {{
            position: absolute; right: 48px; top: 18px; z-index: 0;
            color: rgba(20,33,61,.055); font: 800 72px/1 "Segoe UI", sans-serif;
            letter-spacing: -.06em; pointer-events: none;
        }}
        .finding-slide .page-ghost {{ display:none; }}
        .cover-meta-panel {{
            position: absolute; padding: 22px 24px; border-radius: 10px;
            background: linear-gradient(135deg, rgba(255,255,255,.105), rgba(255,255,255,.045));
            border: 1px solid rgba(184,221,225,.18); backdrop-filter: blur(18px);
            box-shadow: 0 18px 36px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.05);
        }}
        .cover-model-label {{
            position: absolute; z-index: 12; top: 16px; right: 16px;
            padding: 7px 10px; border-radius: 6px;
            color: #EAF7F8; background: rgba(8,126,139,.72);
            border: 1px solid rgba(255,255,255,.18);
            font-size: 10px; font-weight: 700; letter-spacing: .08em;
            pointer-events: none; backdrop-filter: blur(10px);
        }}
        .summary-panel {{
            border: 1px solid var(--line); box-shadow: var(--shadow-panel);
        }}
        .summary-stat-strip {{
            position: absolute; left: 15px; right: 15px; bottom: 15px; z-index: 12;
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
            padding: 8px; border-radius: 12px;
            background: rgba(9,15,23,.72); border: 1px solid rgba(255,255,255,.12);
            backdrop-filter: blur(14px); box-shadow: 0 12px 26px rgba(0,0,0,.22);
        }}
        .summary-stat-item {{
            min-height: 50px; display: flex; align-items: center; gap: 9px;
            padding: 7px 10px; border-radius: 8px; background: rgba(255,255,255,.055);
        }}
        .summary-stat-item .stat-value {{
            position: static !important; color: #FFFFFF !important; font-size: 19px !important;
            font-weight: 800; line-height: 1;
        }}
        .summary-stat-item .stat-label {{ color: #AEB8C6; font-size: 9px; line-height: 1.25; }}
        .summary-side-panel {{
            border-radius: 11px; overflow: hidden; background: var(--surface);
            border: 1px solid var(--line); box-shadow: var(--shadow-panel);
        }}
        .issue-nav-list {{ display: flex; flex-direction: column; gap: 8px; }}
        .issue-nav-card {{
            width: 100%; padding: 10px 11px; text-align: left; cursor: pointer;
            border: 1px solid var(--line); border-radius: 8px; background: #F8FAFB;
            transition: border-color .2s, background .2s, transform .2s, box-shadow .2s;
        }}
        .issue-nav-card:hover {{
            transform: translateY(-1px); border-color: #98D4D8; background: #F2FAFA;
            box-shadow: 0 7px 16px rgba(16,24,40,.08);
        }}
        .issue-nav-card.active {{
            border-color: rgba(8,126,139,.72); background: var(--brand-soft);
            box-shadow: inset 3px 0 0 #087E8B;
        }}
        .issue-nav-select {{ width:100%; padding:0; border:0; background:transparent; text-align:left; cursor:pointer; }}
        .issue-nav-actions {{ display: flex; gap: 7px; margin-top: 8px; }}
        .issue-nav-actions button {{
            flex: 1; padding: 6px 7px; border-radius: var(--radius-sm); border: 1px solid #CAD5DB;
            background: var(--surface); color: #344054; font-size: 9px; font-weight: 750; cursor: pointer;
            transition:color .16s ease, border-color .16s ease, background .16s ease;
        }}
        .issue-nav-actions button:hover {{ border-color:#087E8B; color:#087E8B; background:#F3FBFC; }}
        .model-issue-callout {{
            position: absolute; top: 64px; right: 15px; z-index: 13; width: 230px;
            padding: 11px 13px; border-radius: var(--radius-md);
            color: #F8FAFC; background: rgba(9,15,23,.78);
            border: 1px solid rgba(255,255,255,.14); backdrop-filter: blur(14px);
            box-shadow: 0 12px 24px rgba(0,0,0,.22); opacity: 0; transform: translateY(-5px);
            pointer-events: none; transition: opacity .24s ease, transform .28s ease;
        }}
        .model-issue-callout.visible {{ opacity: 1; transform: translateY(0); }}
        .model-issue-callout .callout-title {{ font-size: 11px; font-weight: 800; line-height: 1.35; }}
        .model-issue-callout .callout-metric {{ margin-top: 7px; color: #FFB4A8; font-size: 10px; font-weight: 700; }}
        .evidence-return {{
            padding: 7px 10px; border: 1px solid #CAD5DB; border-radius: var(--radius-sm);
            color: #475467; background: var(--surface); font-size: 9px; font-weight: 750; cursor: pointer;
            box-shadow:0 2px 5px rgba(16,33,43,.045); transition:all .16s ease;
        }}
        .evidence-return:hover {{ border-color:#087E8B; color:#087E8B; background:#F3FBFC; }}
        .finding-cycle {{
            display:flex; align-items:center; justify-content:space-between; gap:6px;
            padding-top:10px; border-top:1px solid #E4E7EC;
        }}
        .finding-cycle button {{
            min-width:84px; padding:7px 9px; border:1px solid #CAD5DB; border-radius:var(--radius-sm);
            color:#344054; background:#FFFFFF; font-size:9px; font-weight:800; cursor:pointer;
            transition:all .16s ease;
        }}
        .finding-cycle button:hover:not(:disabled) {{ border-color:#087E8B; color:#087E8B; background:#F3FBFC; }}
        .finding-cycle button:disabled {{ opacity:.38; cursor:default; }}
        .finding-cycle span {{ color:#667085; font-size:9px; font-weight:700; }}
        .trace-backdrop {{
            display: none; position: fixed; inset: 0; z-index: 10020;
            background: rgba(9,15,23,.58); backdrop-filter: blur(4px);
        }}
        .trace-backdrop.open {{ display: block; }}
        .trace-drawer {{
            position: absolute; top: 0; right: 0; width: min(560px, 92vw); height: 100%;
            display: flex; flex-direction: column; background: var(--paper);
            box-shadow: -24px 0 60px rgba(0,0,0,.24);
        }}
        .trace-drawer-head {{ padding: 21px 24px; color: #FFFFFF; background: linear-gradient(145deg,#14213D,#0E2534); }}
        .trace-drawer-body {{ flex: 1; overflow: auto; padding: 22px 24px 32px; }}
        .trace-chain {{ position: relative; padding-left: 28px; }}
        .trace-chain::before {{
            content: ""; position: absolute; left: 8px; top: 10px; bottom: 14px; width: 2px;
            background: linear-gradient(#087E8B, #B8DDE1);
        }}
        .trace-step {{
            position: relative; margin-bottom: 13px; padding: 13px 14px;
            border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--surface);
            box-shadow: 0 5px 14px rgba(16,33,43,.04);
        }}
        .trace-step::before {{
            content: ""; position: absolute; left: -27px; top: 18px;
            width: 10px; height: 10px; border-radius: 50%; background: #087E8B;
            box-shadow: 0 0 0 4px #DDF3F4;
        }}
        .trace-step-label {{ color: #087E8B; font-size: 9px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }}
        .trace-step-value {{ margin-top: 5px; color: #344054; font-size: 12px; line-height: 1.5; word-break: break-word; }}
        .trace-step-action {{
            margin-top:10px; padding:7px 10px; border:1px solid #8CC7CD; border-radius:7px;
            color:#087E8B; background:#F3FBFC; font-size:11px; font-weight:800; cursor:pointer;
        }}
        .trace-step-action:hover {{ color:#FFFFFF; background:#087E8B; }}
        .trace-tech-details {{
            margin-top:16px; border:1px solid #D8DEE8; border-radius:10px; background:#FFFFFF; overflow:hidden;
        }}
        .trace-tech-details summary {{
            padding:12px 14px; color:#475467; background:#F2F4F7; font-size:12px; font-weight:800; cursor:pointer;
        }}
        .trace-tech-grid {{ padding:4px 14px 13px; }}
        .trace-tech-row {{ padding:9px 0; border-bottom:1px solid #EAECF0; }}
        .trace-tech-row:last-child {{ border-bottom:0; }}
        .trace-tech-label {{ color:#667085; font-size:10px; font-weight:800; letter-spacing:.04em; }}
        .trace-tech-value {{ margin-top:3px; color:#344054; font:10px/1.5 Consolas,monospace; word-break:break-all; }}
        .stat-card {{
            overflow: hidden; border-radius: var(--radius-md); border: 1px solid rgba(16,24,40,.05);
            box-shadow: 0 5px 12px rgba(16,24,40,.055);
            transition: transform .25s ease, box-shadow .25s ease;
        }}
        .stat-card:hover {{ transform: translateY(-2px); box-shadow: 0 10px 18px rgba(16,24,40,.10); }}
        .stat-value {{ font-variant-numeric: tabular-nums; }}
        .finding-metric-surface {{
            background: linear-gradient(135deg, #F7FAFC, #EEF4F7);
            border: 1px solid var(--line); border-radius: var(--radius-md);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.9);
        }}
        .evidence-file {{
            color: #98A2B3; font: 9px/1.2 ui-monospace, Consolas, monospace;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }}
        .finding-code-chip {{
            max-width:270px; padding:6px 10px; border:1px solid #C8E5E6; border-radius:999px;
            color:#087E8B; background:#EDF8F8; font:700 8.5pt/1.1 ui-monospace,Consolas,monospace;
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
        }}
        .severity-badge {{
            display:flex; align-items:center; justify-content:center; gap:7px; border-radius:999px;
            font-size:8.5pt; font-weight:750; letter-spacing:.02em;
            box-shadow:none;
        }}
        .severity-badge::before {{
            content:""; width:6px; height:6px; flex:0 0 6px;
            border-radius:50%; background:currentColor;
        }}
        .finding-side-panel {{
            border-radius:var(--radius-md); background:var(--surface);
            border:1px solid var(--line); box-shadow:var(--shadow-panel);
        }}
        .finding-accent {{ border-radius:3px; box-shadow:none; }}
        .summary-primary-action, .summary-secondary-action {{
            min-height:36px; border:1px solid #CAD5DB !important;
            border-radius:var(--radius-sm) !important;
            color:#14213D !important; background:#F8FAFC !important;
            box-shadow:none !important; font-size:9pt !important; font-weight:800 !important;
            transition:color .16s ease, border-color .16s ease, background .16s ease, transform .16s ease !important;
        }}
        .summary-primary-action:hover, .summary-secondary-action:hover {{
            color:#087E8B !important; border-color:#8CC7CD !important;
            background:#F3FBFC !important; transform:translateY(-1px);
        }}
        .gallery-button {{
            border-radius:var(--radius-sm) !important; background:rgba(9,15,23,.82) !important;
            transition:transform .16s ease, background .16s ease, box-shadow .16s ease;
        }}
        .gallery-button:hover {{ transform:translateY(-1px); background:rgba(8,126,139,.92) !important; box-shadow:0 7px 16px rgba(0,0,0,.24) !important; }}
        .risk-level-pill {{
            border-radius:999px; box-shadow:0 4px 10px rgba(180,35,24,.07);
        }}
        .notes-band {{
            border-top:1px solid var(--line) !important; background:linear-gradient(90deg,transparent,rgba(241,245,247,.78),transparent);
        }}
        .conclusion-summary {{
            padding: 19px 21px; border-radius: var(--radius-md);
            color: #17323A; background: linear-gradient(135deg, #EAF8F8, #F3FAFB);
            border-left: 5px solid #087E8B; box-shadow: inset 0 0 0 1px rgba(8,126,139,.10);
        }}
        .conclusion-shell {{
            border-radius:11px; background:rgba(255,255,255,.72);
            border:1px solid var(--line); box-shadow:var(--shadow-panel);
        }}
        .conclusion-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }}
        .conclusion-card {{
            min-height: 260px; padding: 20px 21px; border-radius: var(--radius-md);
            background: var(--surface); border: 1px solid var(--line);
            box-shadow: 0 8px 20px rgba(16,33,43,.05);
        }}
        .conclusion-card h3 {{ margin: 0 0 12px; color: #14213D; font-size: 13.5pt; }}
        .conclusion-card ul {{ margin: 0; padding-left: 18px; color: #344054; font-size: 10.2pt; line-height: 1.58; }}
        .conclusion-card li::marker {{ color: #087E8B; }}
        .cover-slide > .shape-rect {{ z-index: 0; }}
        .cover-slide > .element:not(.shape-rect):not(.webgl-wrapper) {{ z-index: 2; }}
        .cover-slide > .webgl-wrapper {{ z-index: 3; }}
        button {{ font-family: inherit; }}
        button:focus-visible {{
            outline: 3px solid rgba(8,126,139,.30); outline-offset: 2px;
        }}
        .text-box:hover, .cover-slide .text-box:hover {{
            background: transparent; box-shadow: none; cursor: default;
        }}
        .demo-nav {{
            position: fixed; left: 50%; bottom: 17px; z-index: 11000;
            transform: translateX(-50%); min-width: min(560px, calc(100vw - 28px));
            display: grid; grid-template-columns: 44px 1fr 44px; align-items: center; gap: 10px;
            padding: 8px; border: 1px solid rgba(255,255,255,.13); border-radius: 16px;
            color: #FFFFFF; background: rgba(7,13,22,.86);
            box-shadow: 0 18px 44px rgba(0,0,0,.34); backdrop-filter: blur(18px);
        }}
        .demo-nav button {{
            height: 38px; border: 1px solid rgba(255,255,255,.13); border-radius: 10px;
            color: #FFFFFF; background: rgba(255,255,255,.07); cursor: pointer;
            font-size: 18px; font-weight: 800; transition: background .16s ease, transform .16s ease;
        }}
        .demo-nav button:hover:not(:disabled) {{ background: #087E8B; transform: translateY(-1px); }}
        .demo-nav button:disabled {{ opacity: .32; cursor: default; }}
        .demo-nav-copy {{ min-width: 0; display: grid; grid-template-columns: 1fr auto; gap: 4px 14px; align-items: end; }}
        .demo-nav-label {{ overflow: hidden; color: #F8FAFC; font-size: 12px; font-weight: 800; text-overflow: ellipsis; white-space: nowrap; }}
        .demo-nav-count {{ color: #9FB0BC; font: 10px/1.2 ui-monospace, Consolas, monospace; }}
        .demo-progress {{ grid-column: 1 / -1; height: 3px; overflow: hidden; border-radius: 99px; background: rgba(255,255,255,.10); }}
        .demo-progress > span {{ display: block; width: 0; height: 100%; background: linear-gradient(90deg,#55D6BE,#087E8B); transition: width .25s ease; }}
        .cover-primary-action {{
            display: inline-flex; align-items: center; justify-content: center; gap: 9px;
            border: 1px solid rgba(255,255,255,.18); border-radius: 9px;
            color: #FFFFFF; background: linear-gradient(135deg,#0A8E9C,#076A75);
            box-shadow: 0 12px 26px rgba(0,0,0,.24); cursor: pointer;
            font-size: 11pt; font-weight: 800; transition: transform .18s ease, box-shadow .18s ease;
        }}
        .cover-primary-action:hover {{ transform: translateY(-2px); box-shadow: 0 17px 32px rgba(0,0,0,.30); }}
        .finding-toolbar {{
            display: flex; align-items: center; justify-content: flex-end; gap: 8px;
            z-index: 220;
        }}
        .finding-toolbar .evidence-return,
        .finding-rule-button,
        .finding-toolbar .severity-badge {{
            position: static; width: auto; height: 34px; min-width: 0;
            display: inline-flex; align-items: center; justify-content: center;
            padding: 0 13px; border-radius: 999px; white-space: nowrap;
            font-size: 9px; font-weight: 800; line-height: 1;
        }}
        .finding-toolbar .evidence-return,
        .finding-rule-button {{
            color: #475467; background: #FFFFFF; border: 1px solid #CAD5DB;
            box-shadow: 0 2px 6px rgba(16,33,43,.05); cursor: pointer;
            transition: color .16s ease, border-color .16s ease, background .16s ease;
        }}
        .finding-toolbar .evidence-return:hover,
        .finding-rule-button:hover {{
            color: #087E8B; border-color: #8CC7CD; background: #F3FBFC;
        }}
        .finding-toolbar .rules-tooltip {{ position: relative; }}
        .finding-toolbar .rules-content {{ top: 42px !important; }}
        .finding-toolbar .severity-badge {{ box-shadow: none; }}
        ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
        ::-webkit-scrollbar-track {{ background: rgba(148,163,184,.12); border-radius: 10px; }}
        ::-webkit-scrollbar-thumb {{ background: rgba(71,84,103,.34); border-radius: 10px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: rgba(71,84,103,.52); }}

        @media (max-width: 1200px) {{ .slide {{ zoom: .86; }} }}
        @media (max-width: 1000px) {{
            body {{ padding-inline: 16px; gap: 24px; }}
            .slide {{ zoom: .70; }}
        }}
        @media (max-width: 760px) {{ .slide {{ zoom: .52; }} }}
        @media (max-width: 540px) {{
            body {{ padding-inline: 10px; gap: 18px; }}
            .slide {{ zoom: .36; border-radius: 12px; }}
            .demo-nav {{ bottom: 10px; }}
        }}
        @media (prefers-reduced-motion: reduce) {{
            *, *::before, *::after {{
                animation-duration: .01ms !important; animation-iteration-count: 1 !important;
                scroll-behavior: auto !important; transition-duration: .01ms !important;
            }}
        }}

        @page {{ size: 13.333in 7.5in; margin: 0; }}
        @media print {{
            html, body {{ background: #FFFFFF !important; }}
            body {{ padding: 0; gap: 0; display: block; }}
            .demo-nav {{ display: none !important; }}
            .slide {{
                zoom: 1 !important; border: 0; border-radius: 0; box-shadow: none;
                break-after: page; page-break-after: always;
                -webkit-print-color-adjust: exact; print-color-adjust: exact;
            }}
            .slide:last-of-type {{ break-after: auto; page-break-after: auto; }}
        }}
    </style>
    <!-- Three.js r128 and OrbitControls are pinned and embedded for offline delivery. -->
    <script>{three_js}</script>
    <script>{orbit_controls_js}</script>
</head>
<body>
    <nav class="demo-nav" aria-label="报告翻页导航">
        <button id="demoPrev" type="button" aria-label="上一页">‹</button>
        <div class="demo-nav-copy">
            <div id="demoPageLabel" class="demo-nav-label">报告封面</div>
            <div id="demoPageCount" class="demo-nav-count">01 / 01</div>
            <div class="demo-progress"><span id="demoProgressBar"></span></div>
        </div>
        <button id="demoNext" type="button" aria-label="下一页">›</button>
    </nav>
"""

    def add_shape(x, y, w, h, fill, radius=True):
        shape_class = "shape" if radius else "shape-rect"
        return f'<div class="element {shape_class}" style="left:{inch2px(x)}px; top:{inch2px(y)}px; width:{inch2px(w)}px; height:{inch2px(h)}px; background-color:#{fill};"></div>\n'

    def add_text(text, x, y, w, h, size=16, color="17202A", bold=False, align="left", valign="center", full=False):
        weight = "bold" if bold else "normal"
        align_items = "flex-start" if align == "left" else ("center" if align == "center" else "flex-end")
        justify_content = "flex-start" if valign == "top" else ("flex-end" if valign == "bottom" else "center")
        return f'<div class="element text-box" style="left:{inch2px(x)}px; top:{inch2px(y)}px; width:{inch2px(w)}px; font-size:{size}pt; color:#{color}; font-weight:{weight}; align-items:{align_items}; justify-content:{justify_content}; text-align:{align}; line-height:1.2;">{text}</div>\n'

    def add_picture(img_b64, x, y, w, h):
        if not img_b64: return ""
        return f'<div class="element picture-frame" style="left:{inch2px(x)}px; top:{inch2px(y)}px; width:{inch2px(w)}px; height:{inch2px(h)}px;"><img src="{img_b64}" class="img-contain" loading="lazy" decoding="async"></div>\n'

    def add_link_button(text, onclick_js, x, y, w, h):
        return f'<button onclick="{onclick_js}" class="element action-btn" style="left:{inch2px(x)}px; top:{inch2px(y)}px; width:{inch2px(w)}px; height:{inch2px(h)}px; border:none; outline:none;">{text} ↗</button>\n'

    def add_title(title_text, badge_text=""):
        res = f'<div class="element" style="left:{inch2px(0.72)}px; top:{inch2px(0.25)}px; width:{inch2px(10.0)}px;">'
        res += '<div class="section-eyebrow" style="margin-bottom:7px;">详细问题 · 证据复核</div>'
        res += '<div style="display:flex; align-items:center; gap:14px;">'
        res += f'<div style="font-size:24pt; color:#17202A; font-weight:800; line-height:1.12; letter-spacing:-.02em; word-break:break-word; max-width:75%;">{title_text}</div>'
        if badge_text:
            res += f'<div class="finding-code-chip">{badge_text}</div>'
        res += '</div></div>\n'
        return res

    def add_footer(page):
        res = add_text(f"Hermes DFM · {scope_id}", 0.65, 7.14, 5.5, 0.2, size=8, color="667085")
        res += add_text(str(page), 12.05, 7.14, 0.55, 0.2, size=8, color="667085", align="right")
        return res

    # --- SLIDE 1: COVER ---
    html += '<div class="slide cover-slide" id="report-cover">\n'
    html += add_shape(0, 0, 13.333, 7.5, "14213D", radius=False)
    html += '<div class="element section-eyebrow" style="left:84px; top:62px; color:#7BD1D8;">Hermes 工程智能 · 决策审阅</div>\n'
    html += add_shape(0.88, 1.1, 0.62, 0.07, "087E8B", radius=False)
    html += add_text("DFM 分析报告", 0.86, 1.35, 5.55, 0.85, size=39, color="FFFFFF", bold=True)
    html += add_text("注塑成型 · 可制造性评估", 0.89, 2.23, 5.2, 0.42, size=16, color="B8DDE1")

    final_part_name = obs_part_name if obs_part_name else metadata.get('part_name', '未命名零件')
    html += f'<div class="element cover-meta-panel" style="left:{inch2px(0.88)}px; top:{inch2px(3.05)}px; width:{inch2px(5.35)}px; height:{inch2px(2.25)}px; display:flex; flex-direction:column; gap:14px; overflow:hidden;">'
    html += '<div style="color:#8FB9BD; font-size:9pt; font-weight:700; letter-spacing:.12em;">PART / PROCESS PROFILE</div>'
    html += f'<div style="font-size:21pt; color:#FFFFFF; font-weight:800; line-height:1.2; word-break:break-word;">{final_part_name}</div>'
    html += f'<div style="display:grid; grid-template-columns:84px 1fr; gap:7px 12px; font-size:11pt; line-height:1.35;"><span style="color:#8FB9BD;">工艺</span><span style="color:#FFFFFF; font-weight:600;">{process}</span><span style="color:#8FB9BD;">规则范围</span>'
    html += f'<button onclick="openRuleLibrary()" style="background:transparent; border:none; color:#B8DDE1; padding:0; text-align:left; font-size:11pt; cursor:pointer; text-decoration:underline; text-underline-offset:3px;">{scope_id}</button></div>'
    html += '</div>\n'
    html += f'<button class="element cover-primary-action" onclick="document.getElementById(\'analysis-summary\').scrollIntoView({{behavior: \'smooth\', block: \'center\'}})" style="left:{inch2px(0.88)}px; top:{inch2px(5.62)}px; width:{inch2px(2.32)}px; height:{inch2px(0.52)}px;">进入分析总览 <span aria-hidden="true">→</span></button>\n'
    html += '<div class="element" style="left:86px; bottom:58px; color:#7E8DA4; font-size:9px; letter-spacing:.16em;">MANUFACTURING READINESS REVIEW</div>\n'
    html += f'<div class="webgl-wrapper" style="left:{inch2px(6.82)}px; top:{inch2px(0.58)}px; width:{inch2px(5.86)}px; height:{inch2px(6.28)}px;"><div class="cover-model-label">INTERACTIVE 3D MODEL</div><div class="webgl-container" data-mode="null"></div></div>\n'
    html += '</div>\n'

    # --- SLIDE 2: SUMMARY ---
    html += '<div class="slide summary-slide" id="analysis-summary">\n'
    html += add_shape(0, 0, 13.333, 7.5, "F7F8FA", radius=False)
    html += '<div class="page-ghost">02</div>\n'
    html += '<div class="element section-eyebrow" style="left:66px; top:31px;">执行概览</div>\n'
    html += add_text("分析摘要", 0.65, 0.49, 10.8, 0.55, size=26, bold=True, valign="top")
    html += add_text("几何计算、问题分布与模型信息的一页总览", 0.67, 1.02, 11.7, 0.3, size=10, color="667085")

    # Model-centric summary: live 3D is the primary visual, facts sit around it.
    from collections import Counter
    severities = [str(i.get("severity", "info")).lower() for i in issues]
    counts = Counter(severities)
    risk_cards = [("问题总数", len(issues), "14213D", "F2F4F7"), ("高风险", counts["critical"] + counts["high"], "D92D20", "FEF3F2"), ("中风险", counts["medium"], "DC6803", "FFF8EB"), ("低风险", counts["low"], "1570A6", "EFF8FF")]

    html += f'<div class="webgl-wrapper" style="left:{inch2px(0.65)}px; top:{inch2px(1.48)}px; width:{inch2px(8.35)}px; height:{inch2px(5.38)}px;">'
    html += f'<div class="glass-panel"><button class="glass-btn active" onclick="activateSummary3D(\'thickness\')">壁厚场</button><button class="glass-btn" onclick="activateSummary3D(\'draft\')">拔模场</button></div>'
    html += '<div id="activeModeLabel" class="cover-model-label">WALL THICKNESS · DEFECT ISOLATION</div>'
    html += '<div id="modelIssueCallout" class="model-issue-callout"><div class="callout-title"></div><div class="callout-metric"></div></div>'
    html += f'<div class="legend"><div class="legend-item"><div class="legend-color" style="background:#F21F12"></div><span class="legend-text">超限缺陷实体</span></div><div class="legend-item"><div class="legend-color" style="background:#9AA6B2"></div><span class="legend-text">半透明结构外壳</span></div></div>'
    html += '<div class="summary-stat-strip">'
    for label, value, color, fill in risk_cards:
        html += f'<div class="summary-stat-item"><div style="width:4px; align-self:stretch; border-radius:4px; background:#{color};"></div><div class="stat-value" data-target="{value}">{value}</div><div class="stat-label">{label}</div></div>'
    html += '</div>'
    html += f'<div class="webgl-container" data-mode="thickness"></div></div>\n'

    # Right: compact information rail.
    html += f'<div class="element summary-side-panel" style="left:{inch2px(9.2)}px; top:{inch2px(1.48)}px; width:{inch2px(3.45)}px; height:{inch2px(5.38)}px;"></div>\n'

    # Issue navigator: selecting a card drives the model, evidence page and trace drawer.
    html += f'<div class="element section-eyebrow" style="left:{inch2px(9.5)}px; top:{inch2px(1.74)}px;">问题导航</div>\n'
    html += add_text("重点缺陷", 9.48, 1.98, 2.75, 0.32, size=14, bold=True)
    issue_nav_html = f'<div class="element issue-nav-list" style="left:{inch2px(9.48)}px; top:{inch2px(2.38)}px; width:{inch2px(2.85)}px; height:{inch2px(1.64)}px; overflow-y:auto; padding-right:3px;">'
    for nav_issue in list(issue_ui_data.values())[:4]:
        nav_id = nav_issue["id"]
        nav_operator = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "="}.get(nav_issue["operator"], nav_issue["operator"])
        nav_metric = f'实测 {nav_issue["actual"]} {nav_issue["unit"]} · 要求 {nav_operator} {nav_issue["expected"]} {nav_issue["unit"]}'
        issue_nav_html += f'''
        <div class="issue-nav-card" data-issue-nav="{nav_id}">
            <button class="issue-nav-select" aria-pressed="false" onclick="selectIssueFromSummary('{nav_id}', true)">
                <div style="font-size:9.5pt; color:#17202A; font-weight:800; line-height:1.25;">{nav_issue["title"]}</div>
                <div style="margin-top:4px; color:#D92D20; font-size:8pt; font-weight:700;">{nav_metric}</div>
            </button>
            <div class="issue-nav-actions">
                <button onclick="event.stopPropagation(); goToEvidence('{nav_id}')">查看证据</button>
                <button onclick="event.stopPropagation(); openTraceDrawer('{nav_id}')">完整溯源</button>
            </div>
        </div>
        '''
    issue_nav_html += '</div>'
    html += issue_nav_html

    # Section C: Model Info (Driven by Insights Data Contract)
    html += f'<div class="element" style="left:{inch2px(9.48)}px; top:{inch2px(4.14)}px; width:{inch2px(2.85)}px; border-top:1px solid #E4E7EC;"></div>\n'
    html += f'<div class="element section-eyebrow" style="left:{inch2px(9.5)}px; top:{inch2px(4.36)}px;">模型概况</div>\n'
    html += add_text("模型信息", 9.48, 4.60, 2.2, 0.3, size=14, bold=True)

    rows = []

    # Try to get translations from insights
    meta_translations = insights.get("metadata_translations", {})
    final_tolerance = meta_translations.get("tolerance", obs_tolerance)
    final_note = meta_translations.get("note", obs_note)

    if final_tolerance: rows.append(("全局公差 (2D)", final_tolerance))
    if final_note: rows.append(("技术要求 (2D)", final_note))

    model_metrics = insights.get("model_metrics", [])
    for metric in model_metrics:
        rows.append((metric["label"], metric["value"]))
        if len(rows) == 10: break

    info_html = f'<div class="element" style="left:{inch2px(9.48)}px; top:{inch2px(4.98)}px; width:{inch2px(2.85)}px; height:{inch2px(1.16)}px; display:flex; flex-direction:column; gap:6px; overflow-y:auto; overflow-x:hidden;">'
    if not rows:
        info_html += '<div style="font-size:9pt; color:#667085;">报告中未提供可直接展示的模型统计字段。</div>'
    else:
        for key, value in rows:
            info_html += f'''
            <div style="display:flex; justify-content:space-between; align-items:flex-start; border-bottom:1px dashed #E2E8F0; padding-bottom:4px;">
                <div style="font-size:8pt; color:#667085; width:47%; flex-shrink:0;">{key}</div>
                <div style="font-size:8pt; font-weight:bold; color:#17202A; width:49%; text-align:right; word-break:break-word;">{value}</div>
            </div>
            '''
        # (Buttons moved outside)

    info_html += '</div>\n'

    # Add the buttons OUTSIDE the scrolling div, right below it
    info_html += f'<div class="element" style="left:{inch2px(9.48)}px; top:{inch2px(6.28)}px; width:{inch2px(2.85)}px; display:flex; gap:8px;">'
    info_html += '<button class="summary-primary-action" onclick="openEmbeddedPdf()" style="flex:1; padding:8px 0; color:white; border:none; cursor:pointer; font-size:10pt;">2D 图纸</button>'
    if len(issues_without_evidence) > 0:
        info_html += f'<button class="summary-secondary-action" onclick="openNoEvidenceModal()" style="flex:1.45; padding:8px 0; color:#14213D; border:1px solid #D0D5DD; cursor:pointer; font-size:8.5pt; white-space:nowrap;">无证据问题 ({len(issues_without_evidence)})</button>'
    info_html += '</div>\\n'

    html += info_html

    html += add_footer(2)
    html += '</div>\n'

    # --- SLIDES 4+: FINDINGS ---
    page = 3
    SEVERITY = {"critical": "B42318", "high": "D92D20", "medium": "DC6803", "low": "1570A6", "info": "475467"}
    SEVERITY_SURFACE = {
        "critical": ("B42318", "FEF3F2", "FECDCA"),
        "high": ("B42318", "FEF3F2", "FECDCA"),
        "medium": ("B54708", "FFFAEB", "FEDF89"),
        "low": ("175CD3", "EFF8FF", "B2DDFF"),
        "info": ("344054", "F2F4F7", "D0D5DD"),
    }

    severity_labels = {"critical": "严重", "high": "高风险", "medium": "中风险", "low": "低风险", "info": "需关注", "unclassified": "需关注"}
    evidence_issue_ids = [str(item.get("id") or "DFM") for item in issues_with_evidence]
    for issue_index, issue in enumerate(issues_with_evidence):
        severity = str(issue.get("severity") or "info").lower()
        color = SEVERITY.get(severity, SEVERITY["info"])
        badge_text, badge_bg, badge_border = SEVERITY_SURFACE.get(severity, SEVERITY_SURFACE["info"])
        severity_label = severity_labels.get(severity, "需关注")
        issue_id = str(issue.get("id") or "DFM")
        title = str(issue.get("title") or issue.get("code") or "DFM 问题")

        raw_images = issue.get("images")
        values = list(raw_images) if isinstance(raw_images, list) else []
        if issue.get("image") and issue.get("image") not in values:
            values.append(issue.get("image"))

        all_imgs = []
        for img_name in values:
            if img_name and (evidence_root / img_name).is_file():
                all_imgs.append(img_name)

        slide_imgs = all_imgs[:3]

        # Store full base64 images for the gallery
        gallery_data[issue_id] = [get_base64_image(img, evidence_root) for img in all_imgs]

        html += f'<div class="slide finding-slide" id="issue-{issue_id}" data-issue-id="{issue_id}">\n'
        html += add_shape(0, 0, 13.333, 7.5, "F7F8FA", radius=False)
        html += f'<div class="page-ghost">{page:02d}</div>\n'
        # Use LLM insights if available
        issue_insights = insights.get("issues", {}).get(issue_id, {})
        if issue_insights.get("human_title"):
            title = issue_insights.get("human_title")

        html += add_title(title, str(issue.get("code") or ""))
        html += f'''<div class="element finding-toolbar" style="right:{inch2px(0.67)}px; top:{inch2px(0.36)}px;">
            <button class="evidence-return" onclick="returnToSummary()">← 分析摘要</button>
            <div class="rules-tooltip">
                <button class="finding-rule-button" type="button">判定规则</button>
                <div class="rules-content" style="display:none; position:absolute; right:0; width:260px; background:#14213D; color:white; padding:15px; border-radius:8px; box-shadow:0 12px 30px rgba(0,0,0,0.25); font-size:12px; line-height:1.8;">
                <div style="font-weight:bold; font-size:14px; margin-bottom:8px; color:#087E8B; border-bottom:1px solid #334155; padding-bottom:5px;">证据页规则</div>
                <ul style="margin:0; padding-left:15px; color:#E7ECF3;">
                    <li>每个问题最多 3 张证据图</li>
                    <li>固定顺序：正视、剖视、斜视</li>
                    <li>检测指标直接来自分析 JSON</li>
                    <li>无证据的问题保留在 JSON/MD</li>
                </ul>
                </div>
            </div>
            <div class="severity-badge" style="color:#{badge_text}; background:#{badge_bg}; border:1px solid #{badge_border};">{severity_label}</div>
        </div>\n'''

        html += f'<div class="element finding-side-panel" style="left:{inch2px(0.7)}px; top:{inch2px(1.35)}px; width:{inch2px(3.28)}px; height:{inch2px(5.45)}px;"></div>\n'
        html += f'<div class="element finding-accent" style="left:{inch2px(0.7)}px; top:{inch2px(1.35)}px; width:{inch2px(0.055)}px; height:{inch2px(5.45)}px; background:#{color};"></div>\n'
        html += f'<div class="element section-eyebrow" style="left:{inch2px(1.0)}px; top:{inch2px(1.58)}px;">问题 {page - 2:02d} · 诊断</div>\n'
        html += add_text("问题说明", 1.0, 1.82, 1.6, 0.35, size=16, bold=True)
        message = str(insights.get("issues", {}).get(issue_id, {}).get("translated_message", issue.get("message", "未提供问题说明")))
        if "draft" in str(issue.get("code", "")).lower():
            message = re.sub(r'(\d+(?:\.\d+)?)\s*mm\b', r'\1°', message)
        html += f'<div class="element" style="left:{inch2px(1.0)}px; top:{inch2px(2.2)}px; width:{inch2px(2.66)}px; height:{inch2px(1.03)}px; overflow-y:auto; overflow-x:hidden; word-break:break-word; font-size:11pt; color:#344054; line-height:1.5;">{message}</div>\n'

        html += add_text("检测指标", 1.0, 3.45, 1.6, 0.35, size=16, bold=True)
        html += f'<div class="element finding-metric-surface" style="left:{inch2px(0.94)}px; top:{inch2px(3.82)}px; width:{inch2px(2.80)}px; height:{inch2px(2.42)}px;"></div>\n'

        metric = issue.get("metric", {})
        actual = metric.get("actual", "N/A")
        expected = metric.get("expected", "N/A")
        op = metric.get("operator", "")
        op_display = {">=": "≥", "<=": "≤", "==": "="}.get(op, op)

        if isinstance(actual, (int, float)): actual = round(actual, 3)
        if isinstance(expected, (int, float)): expected = round(expected, 3)

        unit = ""
        m_ids = metric.get("measurement_ids", [])
        if m_ids and len(m_ids) > 0:
            if "_mm-" in m_ids[0]: unit = "mm"
            elif "_deg-" in m_ids[0]: unit = "°"

        metrics_html = f'<div class="element" style="left:{inch2px(1.08)}px; top:{inch2px(4.0)}px; width:{inch2px(2.5)}px; display:flex; flex-direction:column; gap:8px; font-family:\'Segoe UI\', system-ui, sans-serif;">'
        if actual != "N/A" or expected != "N/A":
            param_name = "检测参数 (Measurement)"
            code_str = str(issue.get("code", "")).lower()
            if "thickness" in code_str:
                param_name = "实测壁厚 (Actual Thickness)"
            elif "draft" in code_str:
                param_name = "实测拔模角 (Actual Draft Angle)"

            metrics_html += f'''
            <div style="font-size:10pt; color:#6B7280; font-weight:600; letter-spacing:0.05em;">{param_name}</div>

            <div style="display:flex; align-items:baseline; gap:8px;">
                <span style="font-size:20pt; font-weight:800; color:#111827; line-height:1; letter-spacing:-0.02em;">{actual}</span>
                <span style="font-size:12pt; color:#6B7280; font-weight:500;">{unit}</span>
            </div>

            <div style="display:flex; gap:16px; margin-top:4px; align-items:center;">
                <div style="display:flex; align-items:center; gap:6px;">
                    <div style="width:8px; height:8px; border-radius:50%; background-color:#EF4444; box-shadow:0 0 4px rgba(239,68,68,0.5);"></div>
                    <span style="font-size:10pt; color:#374151; font-weight:500;">未达标</span>
                </div>
                <div style="width:1px; height:12px; background-color:#D1D5DB;"></div>
                <div style="display:flex; align-items:center; gap:6px;">
                    <span style="font-size:10pt; color:#6B7280;">判定要求：{op_display} {expected} {unit}</span>
                </div>
            </div>
            '''

            rule_id = metric.get("rule_id", "")
            if rule_id:
                metrics_html += f'''
                <div style="margin-top:12px; padding-top:12px; border-top:1px solid #E5E7EB;">
                    <div style="display:flex; flex-direction:column; align-items:flex-start; gap:8px;">
                        <button onclick="openTraceDrawer('{issue_id}')" style="background:#F8FAFC; border:1px solid #D1D5DB; padding:6px 9px; border-radius:6px; font-size:8.5pt; cursor:pointer; color:#475467; font-weight:700; transition:all 0.2s;" onmouseover="this.style.background='#F3FBFC'; this.style.color='#087E8B'; this.style.borderColor='#8CC7CD'" onmouseout="this.style.background='#F8FAFC'; this.style.color='#475467'; this.style.borderColor='#D1D5DB'">
                            查看判定依据与完整溯源
                        </button>
                    </div>
                </div>
                '''
        else:
            metrics_html += '<div style="font-size:10pt; color:#667085;">无结构化指标</div>'

        metrics_html += '</div>\n'
        html += metrics_html

        evidence_x, evidence_w = 4.25, 8.37

        # One dominant evidence view plus a supporting-view rail.
        if slide_imgs:
            hero_path = slide_imgs[0]
            hero_w = 5.55 if len(slide_imgs) > 1 else evidence_w
            html += add_shape(evidence_x, 1.35, hero_w, 5.45, "FFFFFF")
            html += f'<div class="element section-eyebrow" style="left:{inch2px(evidence_x + 0.18)}px; top:{inch2px(1.56)}px;">主要证据</div>\n'
            html += add_text("主证据视图", evidence_x + 0.18, 1.79, hero_w - 0.36, 0.32, size=13, bold=True, align="left")
            html += f'<div class="element evidence-file" style="left:{inch2px(evidence_x + 0.18)}px; top:{inch2px(2.10)}px; width:{inch2px(hero_w - 0.36)}px;">{os.path.basename(hero_path)}</div>\n'
            html += add_picture(get_base64_image(hero_path, evidence_root), evidence_x + 0.18, 2.31, hero_w - 0.36, 4.27)

            thumb_x = evidence_x + hero_w + 0.18
            thumb_w = evidence_w - hero_w - 0.18
            secondary = slide_imgs[1:3]
            for index, img_path in enumerate(secondary):
                thumb_y = 1.35 + index * 2.81
                html += add_shape(thumb_x, thumb_y, thumb_w, 2.64, "FFFFFF")
                html += add_text(f"辅助证据 {index + 2:02d}", thumb_x + 0.13, thumb_y + 0.14, thumb_w - 0.26, 0.28, size=10, bold=True, align="left")
                html += f'<div class="element evidence-file" style="left:{inch2px(thumb_x + 0.13)}px; top:{inch2px(thumb_y + 0.43)}px; width:{inch2px(thumb_w - 0.26)}px;">{os.path.basename(img_path)}</div>\n'
                html += add_picture(get_base64_image(img_path, evidence_root), thumb_x + 0.13, thumb_y + 0.66, thumb_w - 0.26, 1.78)

            if len(all_imgs) > 3:
                btn_w = 1.55
                btn_h = 0.35
                # Keep the gallery control in the hero-card header instead of
                # floating over the secondary evidence image below it.
                btn_x = evidence_x + hero_w - btn_w - 0.18
                btn_y = 1.50
                html += f'<button onclick="openGallery(\'{issue_id}\')" class="element gallery-button" style="left:{inch2px(btn_x)}px; top:{inch2px(btn_y)}px; width:{inch2px(btn_w)}px; height:{inch2px(btn_h)}px; font-size:9pt; font-weight:bold; color:white; border:1px solid rgba(255,255,255,.18); cursor:pointer; backdrop-filter:blur(8px); box-shadow:0 4px 10px rgba(0,0,0,.22);">全部证据 · {len(all_imgs)}</button>\n'

        previous_issue_id = evidence_issue_ids[issue_index - 1] if issue_index > 0 else None
        next_issue_id = evidence_issue_ids[issue_index + 1] if issue_index + 1 < len(evidence_issue_ids) else None
        previous_action = f'onclick="goToEvidence(\'{previous_issue_id}\')"' if previous_issue_id else 'disabled'
        next_action = f'onclick="goToEvidence(\'{next_issue_id}\')"' if next_issue_id else 'disabled'
        html += f'''<div class="element finding-cycle" style="left:{inch2px(1.0)}px; top:{inch2px(6.31)}px; width:{inch2px(2.66)}px;">
            <button {previous_action}>‹ 上一问题</button>
            <span>{issue_index + 1} / {len(evidence_issue_ids)}</span>
            <button {next_action}>下一问题 ›</button>
        </div>\n'''

        html += add_footer(page)
        html += '</div>\n'
        page += 1

    # --- FINAL SLIDE: AI SUMMARY (AI 综合评估报告) ---
    global_insights = insights.get("global", {})
    assessment_level = global_insights.get("assessment_level", "AI 综合评估")
    html += '<div class="slide conclusion-slide" id="report-conclusion">\n'
    html += add_shape(0, 0, 13.333, 7.5, "F7F8FA", radius=False)
    html += f'<div class="page-ghost">{page:02d}</div>\n'
    html += '<div class="element section-eyebrow" style="left:66px; top:31px;">评估与行动计划</div>\n'
    html += add_text("综合评估与优化建议", 0.65, 0.49, 9.3, 0.55, size=26, bold=True, valign="top")
    html += add_text("基于全局拓扑、缺陷分布与证据结果的智能解读", 0.67, 1.02, 9.4, 0.3, size=10, color="667085")
    html += f'<div class="element risk-level-pill" style="right:188px; top:49px; padding:8px 14px; color:#B42318; background:#FEF3F2; border:1px solid #FECDCA; font-size:10pt; font-weight:800;">{assessment_level}</div>\n'

    # Full Width AI Summary Content
    html += f'<div class="element conclusion-shell" style="left:{inch2px(0.65)}px; top:{inch2px(1.45)}px; width:{inch2px(12.0)}px; height:{inch2px(5.35)}px;"></div>\n'

    summary_paragraph = global_insights.get("summary_paragraph", "等待 AI 分析...")

    core_risks_html = ""
    for risk in global_insights.get("core_risks", []):
        core_risks_html += f'<li style="margin-bottom:8px;"><strong>{risk.get("title")}</strong>：{risk.get("description")}</li>'

    optimization_roadmap_html = ""
    for opt in global_insights.get("optimization_roadmap", []):
        optimization_roadmap_html += f'<li style="margin-bottom:8px;"><strong>{opt.get("title")}</strong>：{opt.get("description")}</li>'

    llm_mock_text = f'''
<div class="conclusion-summary">
    <div class="section-eyebrow" style="margin-bottom:7px;">综合评估</div>
    <div style="font-size:11pt; line-height:1.6; font-weight:600;">{summary_paragraph}</div>
</div>
<div class="conclusion-grid">
    <section class="conclusion-card">
        <h3>核心风险点</h3>
        <ul>{core_risks_html}</ul>
    </section>
    <section class="conclusion-card">
        <h3>建议行动路线</h3>
        <ul>{optimization_roadmap_html}</ul>
    </section>
</div>
'''
    html += f'<div class="element" style="left:{inch2px(0.94)}px; top:{inch2px(1.72)}px; width:{inch2px(11.45)}px; height:{inch2px(4.56)}px; overflow-y:auto; overflow-x:hidden; padding-right:6px;">{llm_mock_text}</div>\n'

    # Fetch traceability info
    input_sha256 = "Unknown"
    try:
        with open(geometry_path, 'r', encoding='utf-8') as geo_f:
            geo_data = json.load(geo_f)
            input_sha256 = geo_data.get('input_sha256', '')
    except Exception:
        pass

    engine_ver = "Unknown"
    if len(issues) > 0 and isinstance(issues[0].get("metric"), dict):
        engine_ver = issues[0]["metric"].get("algorithm_version", "Unknown")

    # Tiny Horizontal Footer for System Notes
    notes_text = f"注：共识别 {len(issues)} 个问题，其中 {len(issues_with_evidence)} 个提供可视证据 ｜ 阈值与评级来自 dfm_report.json ｜ AI 结论请结合工程经验复核"
    trace_text = f"溯源快照 ｜ 引擎：{engine_ver} ｜ CAD 哈希：{input_sha256}"

    html += f'<div class="element notes-band" style="left:{inch2px(0.94)}px; top:{inch2px(6.34)}px; width:{inch2px(11.45)}px; height:{inch2px(0.38)}px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:3px; padding-top:7px;">'
    html += f'<div style="font-size:9pt; color:#667085; letter-spacing:0.05em;">{notes_text}</div>'
    html += f'<div style="font-size:8pt; color:#4B5563; font-family:monospace; letter-spacing:0.02em;">{trace_text}</div>'
    html += '</div>\n'

    html += add_footer(page)
    html += '</div>\n'


    # --- NO EVIDENCE MODAL ---
    skipped_items_html = ""
    global_issues = insights.get("global_issues", [])
    if not global_issues:
        global_issues = issues_without_evidence # Fallback

    for idx, iss in enumerate(global_issues):
        s_title = iss.get("title", iss.get("rule_id", "未命名规则"))
        s_severity = str(iss.get("severity") or "info").lower()
        s_color = SEVERITY.get(s_severity, SEVERITY["info"])
        s_severity_label = severity_labels.get(s_severity, "需关注")
        s_desc = iss.get("description", "无详细说明")

        iss_json = json.dumps(iss, ensure_ascii=False)
        iss_b64 = base64.b64encode(iss_json.encode('utf-8')).decode('utf-8')

        skipped_items_html += f'''
        <div onclick="openRawJson('{iss_b64}')" style="cursor:pointer; background:white; border-left:4px solid #{s_color}; padding:15px; border-radius:6px; margin-bottom:15px; box-shadow:0 2px 5px rgba(0,0,0,0.05); transition: transform 0.1s, box-shadow 0.1s;" onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 4px 10px rgba(0,0,0,0.1)'" onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='0 2px 5px rgba(0,0,0,0.05)'">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                <div style="font-weight:bold; font-size:12pt; color:#17202A;">{s_title}</div>
                <div style="background:#{s_color}22; color:#{s_color}; padding:2px 8px; border-radius:12px; font-size:8pt; font-weight:bold;">{s_severity_label}</div>
            </div>
            <div style="font-size:10pt; color:#475467; line-height:1.5;">{s_desc}</div>
        </div>
        '''

    html += f'''
    <div id="noEvidenceModal" role="dialog" aria-modal="true" aria-labelledby="noEvidenceTitle" onclick="closeNoEvidenceModal()" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(15,23,42,0.85); z-index:9999; backdrop-filter:blur(8px); align-items:center; justify-content:center;">
        <div onclick="event.stopPropagation()" style="background:#F7F8FA; width:min(800px, 92vw); max-height:80vh; border-radius:12px; box-shadow:0 25px 50px rgba(0,0,0,0.25); display:flex; flex-direction:column; overflow:hidden;">
            <div style="background:#14213D; color:white; padding:20px 25px; display:flex; justify-content:space-between; align-items:center;">
                <div id="noEvidenceTitle" style="font-size:14pt; font-weight:bold;">无证据问题</div>
                <button onclick="closeNoEvidenceModal()" aria-label="关闭无证据问题" style="background:none; border:none; color:white; font-size:24pt; cursor:pointer; line-height:1;">&times;</button>
            </div>
            <div style="padding:25px; overflow-y:auto; flex-grow:1; background:#F7F8FA;">
                {skipped_items_html}
            </div>
        </div>
    </div>

    <script>
    function openNoEvidenceModal() {{
        document.getElementById('noEvidenceModal').style.display = 'flex';
        document.body.style.overflow = 'hidden';
    }}
    function closeNoEvidenceModal() {{
        document.getElementById('noEvidenceModal').style.display = 'none';
        document.body.style.overflow = '';
    }}
    function openRawJson(b64Data) {{
        try {{
            const decodedData = decodeURIComponent(escape(window.atob(b64Data)));
            const blob = new Blob([decodedData], {{type: 'application/json'}});
            const url = URL.createObjectURL(blob);
            window.open(url, '_blank');
        }} catch (e) {{
            alert("无法解析原始数据: " + e.message);
        }}
    }}
    </script>
    '''

    # Check for PDF to embed
    pdf_path = resources["drawing_pdf_path"]
    pdf_b64 = ""
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode('utf-8')

    html += f"""
    <div id="galleryOverlay" role="dialog" aria-modal="true" aria-label="全部证据图" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; z-index:9999; background:rgba(0,0,0,0.85); backdrop-filter:blur(5px); justify-content:center; align-items:center;" onclick="closeGallery()">
        <img id="galleryImg" alt="当前证据图" style="max-width:90%; max-height:90%; object-fit:contain; box-shadow:0 10px 40px rgba(0,0,0,0.5);" onclick="event.stopPropagation()" />
        <button onclick="prevGalleryImage(event)" aria-label="上一张证据图" style="position:absolute; left:20px; top:50%; transform:translateY(-50%); background:rgba(255,255,255,0.2); color:white; border:none; padding:20px; font-size:24px; cursor:pointer; border-radius:8px;">&#10094;</button>
        <button onclick="nextGalleryImage(event)" aria-label="下一张证据图" style="position:absolute; right:20px; top:50%; transform:translateY(-50%); background:rgba(255,255,255,0.2); color:white; border:none; padding:20px; font-size:24px; cursor:pointer; border-radius:8px;">&#10095;</button>
        <button onclick="closeGallery()" aria-label="关闭证据图" style="position:absolute; top:20px; right:20px; background:transparent; color:white; border:none; font-size:36px; cursor:pointer;">&times;</button>
        <div id="galleryCounter" style="position:absolute; bottom:20px; color:white; font-size:16px;"></div>
    </div>

    <script>
        const ontologyB64 = "{ontology_b64}";
        const galleryData = {json.dumps(gallery_data)};

        function openRuleLibrary() {{
            if (!ontologyB64) {{
                alert("未找到绑定的规则库数据");
                return;
            }}
            try {{
                const byteCharacters = atob(ontologyB64);
                const byteNumbers = new Array(byteCharacters.length);
                for (let i = 0; i < byteCharacters.length; i++) {{
                    byteNumbers[i] = byteCharacters.charCodeAt(i);
                }}
                const byteArray = new Uint8Array(byteNumbers);
                const blob = new Blob([byteArray], {{type: 'application/json'}});
                const blobUrl = URL.createObjectURL(blob);
                window.open(blobUrl, '_blank');
            }} catch(e) {{
                alert("打开规则库时出错: " + e.message);
            }}
        }}

        let currentGalleryImages = [];
        let currentGalleryIndex = 0;

        function openGallery(issueId) {{
            currentGalleryImages = galleryData[issueId] || [];
            if (currentGalleryImages.length === 0) return;
            currentGalleryIndex = 0;
            document.getElementById('galleryOverlay').style.display = 'flex';
            document.body.style.overflow = 'hidden';
            updateGalleryImage();
        }}

        function closeGallery() {{
            document.getElementById('galleryOverlay').style.display = 'none';
            currentGalleryImages = [];
            document.body.style.overflow = '';
        }}

        function prevGalleryImage(e) {{
            e.stopPropagation();
            if (currentGalleryImages.length === 0) return;
            currentGalleryIndex = (currentGalleryIndex - 1 + currentGalleryImages.length) % currentGalleryImages.length;
            updateGalleryImage();
        }}

        function nextGalleryImage(e) {{
            e.stopPropagation();
            if (currentGalleryImages.length === 0) return;
            currentGalleryIndex = (currentGalleryIndex + 1) % currentGalleryImages.length;
            updateGalleryImage();
        }}

        function updateGalleryImage() {{
            document.getElementById('galleryImg').src = currentGalleryImages[currentGalleryIndex];
            document.getElementById('galleryCounter').innerText = '当前视角: ' + (currentGalleryIndex + 1) + ' / ' + currentGalleryImages.length;
        }}

        document.addEventListener('keydown', function(event) {{
            const galleryOpen = document.getElementById('galleryOverlay').style.display === 'flex';
            if (!galleryOpen) return;
            if (event.key === "Escape") closeGallery();
            if (event.key === "ArrowLeft") prevGalleryImage(event);
            if (event.key === "ArrowRight") nextGalleryImage(event);
        }});

        const sceneData = {scene_data_str};
        const thicknessData = {thickness_data_str};
        const draftData = {draft_data_str};
        const ISSUE_UI_MAP = {json.dumps(issue_ui_data, ensure_ascii=False)};

        // Embedded PDF Base64 string
        const embeddedPdfB64 = "{pdf_b64}";

        function openEmbeddedPdf() {{
            if (!embeddedPdfB64) {{
                alert("未找到嵌入的图纸数据");
                return;
            }}
            try {{
                const byteCharacters = atob(embeddedPdfB64);
                const byteNumbers = new Array(byteCharacters.length);
                for (let i = 0; i < byteCharacters.length; i++) {{
                    byteNumbers[i] = byteCharacters.charCodeAt(i);
                }}
                const byteArray = new Uint8Array(byteNumbers);
                const blob = new Blob([byteArray], {{type: 'application/pdf'}});
                const blobUrl = URL.createObjectURL(blob);
                window.open(blobUrl, '_blank');
            }} catch(e) {{
                alert("打开 PDF 时出错: " + e.message);
            }}
        }}

        const thicknessMap = {{}};
        const draftMap = {{}};

        let heatmapMesh = null; // Store reference to the summary mesh for switching

        if (thicknessData.samples) {{
            for (let s of thicknessData.samples) {{
                const match = s.sample_id.match(/face-(\\d+)-t(\\d+)/);
                if (match) thicknessMap[`face-${{match[1]}}-t${{match[2]}}`] = s.value;
            }}
        }}
        if (draftData.samples) {{
            for (let s of draftData.samples) {{
                const match = s.sample_id.match(/face-(\\d+)-v(\\d+)/);
                if (match) draftMap[`face-${{match[1]}}-v${{match[2]}}`] = s.value;
            }}
        }}

        // Industrial neutral palette: the part stays quiet while risk carries color.
        const DEFAULT_COLOR = [0.72, 0.76, 0.80];
        const SAFE_COLOR = [0.34, 0.56, 0.61];
        const WARNING_COLOR = [1.00, 0.64, 0.08];
        const DEFECT_COLOR = [0.95, 0.12, 0.07];
        const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        let colorTweenFrame = null;

        function mixColor(a, b, t) {{
            return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
        }}

        function animateColorAttribute(geometry, nextColors, animated = true) {{
            const target = new Float32Array(nextColors);
            const current = geometry.getAttribute('color');
            if (!current || current.array.length !== target.length || !animated || reduceMotion) {{
                geometry.setAttribute('color', new THREE.Float32BufferAttribute(target, 3));
                geometry.attributes.color.needsUpdate = true;
                return;
            }}

            if (colorTweenFrame) cancelAnimationFrame(colorTweenFrame);
            const start = new Float32Array(current.array);
            const startedAt = performance.now();
            const duration = 560;
            const step = (now) => {{
                const raw = Math.min(1, (now - startedAt) / duration);
                const eased = 1 - Math.pow(1 - raw, 3);
                for (let i = 0; i < target.length; i++) {{
                    current.array[i] = start[i] + (target[i] - start[i]) * eased;
                }}
                current.needsUpdate = true;
                if (raw < 1) colorTweenFrame = requestAnimationFrame(step);
            }};
            colorTweenFrame = requestAnimationFrame(step);
        }}

        function applyColorsToGeometry(geometry, mode, animated = true) {{
            const colors = [];
            const threshold = mode === 'thickness' ? 1.2 : 1.0;

            for (let p of sceneData.primitives) {{
                let pId = p.primitive_id;
                let tris = p.triangles;
                if (!tris) continue;

                for (let t = 0; t < tris.length; t++) {{
                    let triVal = mode === 'thickness' ? thicknessMap[`${{pId}}-t${{t}}`] : undefined;

                    for (let vIdx of tris[t]) {{
                        let val = mode === 'thickness' ? triVal : (mode === 'draft' ? draftMap[`${{pId}}-v${{vIdx}}`] : undefined);
                        let color = DEFAULT_COLOR;

                        if (mode && val !== undefined) {{
                            if (val < threshold) {{
                                const severity = Math.min(1, Math.max(0, 1 - val / Math.max(threshold, 0.0001)));
                                color = mixColor(WARNING_COLOR, DEFECT_COLOR, Math.min(1, severity * 1.45));
                            }} else {{
                                const safeAmount = Math.min(1, (val - threshold) / Math.max(threshold * 2, 1));
                                color = mixColor(DEFAULT_COLOR, SAFE_COLOR, 0.38 + safeAmount * 0.32);
                            }}
                        }}
                        colors.push(color[0], color[1], color[2]);
                    }}
                }}
            }}
            animateColorAttribute(geometry, colors, animated && mode !== null);
            updateDefectOverlay(mode);
        }}

        let globalScene, globalCamera, globalRenderer, globalControls, globalMeshGroup;
        let isRendering = false;
        let activeContainer = null;
        let modelMaxDim = 1;
        let cameraTweenFrame = null;
        let modelCenter = new THREE.Vector3();
        let defectOverlay = null;
        let activeDefectCenter = new THREE.Vector3();
        let currentDefectMode = 'thickness';
        let userActivatedSummary = false;
        let summaryActivatedAt = 0;

        function updateDefectOverlay(mode) {{
            if (!globalMeshGroup) return;
            const baseMesh = globalMeshGroup.children[0];
            const silhouette = globalMeshGroup.children[1];
            const edgeLines = globalMeshGroup.children[2];

            if (defectOverlay) {{
                globalMeshGroup.remove(defectOverlay);
                defectOverlay.geometry.dispose();
                defectOverlay.material.dispose();
                defectOverlay = null;
            }}

            if (!mode || mode === 'null') {{
                activeDefectCenter.set(0, 0, 0);
                baseMesh.material.transparent = false;
                baseMesh.material.opacity = 1;
                baseMesh.material.depthWrite = true;
                baseMesh.material.needsUpdate = true;
                baseMesh.renderOrder = 0;
                silhouette.material.opacity = 0.075;
                edgeLines.material.opacity = 0.24;
                return;
            }}

            const threshold = mode === 'thickness' ? 1.2 : 1.0;
            const defectPositions = [];
            for (const primitive of sceneData.primitives) {{
                if (!primitive.vertices || !primitive.triangles) continue;
                const primitiveId = primitive.primitive_id;
                primitive.triangles.forEach((triangle, triangleIndex) => {{
                    let failed = false;
                    if (mode === 'thickness') {{
                        const value = thicknessMap[`${{primitiveId}}-t${{triangleIndex}}`];
                        failed = value !== undefined && value < threshold;
                    }} else {{
                        failed = triangle.some((vertexIndex) => {{
                            const value = draftMap[`${{primitiveId}}-v${{vertexIndex}}`];
                            return value !== undefined && value < threshold;
                        }});
                    }}
                    if (!failed) return;
                    triangle.forEach((vertexIndex) => {{
                        const vertex = primitive.vertices[vertexIndex];
                        defectPositions.push(vertex[0] - modelCenter.x, vertex[1] - modelCenter.y, vertex[2] - modelCenter.z);
                    }});
                }});
            }}

            baseMesh.material.transparent = true;
            baseMesh.material.opacity = 0.30;
            baseMesh.material.depthWrite = true;
            baseMesh.material.needsUpdate = true;
            baseMesh.renderOrder = 1;
            silhouette.material.opacity = 0.16;
            edgeLines.material.opacity = 0.38;

            if (!defectPositions.length) return;
            const defectGeometry = new THREE.BufferGeometry();
            defectGeometry.setAttribute('position', new THREE.Float32BufferAttribute(defectPositions, 3));
            defectGeometry.computeVertexNormals();
            defectGeometry.computeBoundingBox();
            defectGeometry.boundingBox.getCenter(activeDefectCenter);
            const defectMaterial = new THREE.MeshPhysicalMaterial({{
                color: 0xFF2F18, emissive: 0x5A0903, emissiveIntensity: 0.72,
                side: THREE.DoubleSide, roughness: 0.28, metalness: 0.04,
                clearcoat: 0.82, clearcoatRoughness: 0.18,
                transparent: true, opacity: 0.97, depthTest: true, depthWrite: true,
                polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2
            }});
            defectOverlay = new THREE.Mesh(defectGeometry, defectMaterial);
            defectOverlay.renderOrder = 8;
            defectOverlay.scale.setScalar(1.002);
            globalMeshGroup.add(defectOverlay);
        }}

        function createModelMesh() {{
            const positions = [];
            for (let p of sceneData.primitives) {{
                let verts = p.vertices;
                let tris = p.triangles;
                if (!verts || !tris) continue;
                for (let t = 0; t < tris.length; t++) {{
                    for (let vIdx of tris[t]) {{
                        positions.push(verts[vIdx][0], verts[vIdx][1], verts[vIdx][2]);
                    }}
                }}
            }}

            const geometry = new THREE.BufferGeometry();
            geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
            geometry.computeVertexNormals();
            geometry.computeBoundingBox();
            geometry.boundingBox.getCenter(modelCenter);
            geometry.translate(-modelCenter.x, -modelCenter.y, -modelCenter.z);
            applyColorsToGeometry(geometry, null);

            const material = new THREE.MeshPhysicalMaterial({{
                vertexColors: true, side: THREE.DoubleSide,
                roughness: 0.38, metalness: 0.12, clearcoat: 0.78, clearcoatRoughness: 0.24,
                reflectivity: 0.56
            }});

            const meshGroup = new THREE.Group();
            const mesh = new THREE.Mesh(geometry, material);
            meshGroup.add(mesh);

            const silhouette = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({{
                color: 0x6FD0D6, side: THREE.BackSide, transparent: true,
                opacity: 0.075, depthWrite: false
            }}));
            silhouette.scale.setScalar(1.008);
            meshGroup.add(silhouette);

            const edges = new THREE.EdgesGeometry(geometry, 22);
            const line = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({{ color: 0xDCEBF0, transparent: true, opacity: 0.24 }}));
            meshGroup.add(line);

            heatmapMesh = mesh; // Legacy compat
            return meshGroup;
        }}

        function initGlobal3D() {{
            globalScene = new THREE.Scene();
            const compactViewport = window.innerWidth <= 760;
            globalRenderer = new THREE.WebGLRenderer({{
                antialias: !compactViewport,
                alpha: true,
                powerPreference: 'high-performance'
            }});
            globalRenderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, compactViewport ? 1 : 1.35));
            globalRenderer.outputEncoding = THREE.sRGBEncoding;
            globalRenderer.physicallyCorrectLights = true;
            globalRenderer.toneMapping = THREE.ACESFilmicToneMapping;
            globalRenderer.toneMappingExposure = 1.08;

            globalCamera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);

            globalMeshGroup = createModelMesh();
            globalScene.add(globalMeshGroup);

            const ambientLight = new THREE.AmbientLight(0x9FB4C6, 0.42);
            globalScene.add(ambientLight);

            const hemiLight = new THREE.HemisphereLight(0xEAF6FF, 0x15202B, 1.05);
            hemiLight.position.set(0, 20, 0);
            globalScene.add(hemiLight);

            const dirLight1 = new THREE.DirectionalLight(0xFFF2DF, 1.28);
            dirLight1.position.set(12, 18, 14);
            globalScene.add(dirLight1);

            const dirLight2 = new THREE.DirectionalLight(0xB9DFFF, 0.72);
            dirLight2.position.set(-14, 4, -10);
            globalScene.add(dirLight2);

            const rimLight = new THREE.DirectionalLight(0x61D4DB, 0.78);
            rimLight.position.set(-6, 12, 18);
            globalScene.add(rimLight);

            let box = new THREE.Box3().setFromObject(globalMeshGroup);
            modelMaxDim = Math.max(box.max.x - box.min.x, box.max.y - box.min.y, box.max.z - box.min.z);
            globalCamera.position.set(modelMaxDim*0.88, modelMaxDim*0.68, modelMaxDim*0.94);

            globalControls = new THREE.OrbitControls(globalCamera, globalRenderer.domElement);
            globalControls.enableDamping = true;
            globalControls.dampingFactor = 0.065;
            globalControls.minDistance = modelMaxDim * 0.34;
            globalControls.maxDistance = modelMaxDim * 3.4;

            globalRenderer.domElement.addEventListener('pointerdown', () => {{ globalControls.autoRotate = false; }});
            globalRenderer.domElement.addEventListener('pointerleave', () => {{
                if (!reduceMotion && activeContainer && !String(activeContainer.dataset.mode).startsWith('minimap_')) {{
                    window.setTimeout(() => {{ if (isRendering) globalControls.autoRotate = true; }}, 1000);
                }}
            }});

            animate();
        }}

        let lastRenderAt = 0;
        function animate(now = 0) {{
            requestAnimationFrame(animate);
            if (!isRendering || document.hidden || now - lastRenderAt < 33) return;
            lastRenderAt = now;
            globalControls.update();
            globalRenderer.render(globalScene, globalCamera);
        }}

        document.addEventListener('visibilitychange', () => {{ lastRenderAt = 0; }});

        function tweenCameraForMode(mode) {{
            if (!globalCamera || !globalControls) return;
            const view = mode === 'draft'
                ? [1.04, 0.42, 0.80]
                : [0.78, 0.82, 1.02];
            const lookAt = activeDefectCenter.clone();
            const target = new THREE.Vector3(
                lookAt.x + view[0] * modelMaxDim * 0.82,
                lookAt.y + view[1] * modelMaxDim * 0.82,
                lookAt.z + view[2] * modelMaxDim * 0.82
            );
            if (reduceMotion) {{
                globalCamera.position.copy(target);
                globalControls.target.copy(lookAt);
                globalControls.update();
                return;
            }}
            if (cameraTweenFrame) cancelAnimationFrame(cameraTweenFrame);
            const start = globalCamera.position.clone();
            const startLookAt = globalControls.target.clone();
            const startedAt = performance.now();
            globalControls.autoRotate = false;
            const step = (now) => {{
                const raw = Math.min(1, (now - startedAt) / 680);
                const eased = raw < .5 ? 4 * raw * raw * raw : 1 - Math.pow(-2 * raw + 2, 3) / 2;
                globalCamera.position.lerpVectors(start, target, eased);
                globalControls.target.lerpVectors(startLookAt, lookAt, eased);
                globalControls.update();
                if (raw < 1) {{
                    cameraTweenFrame = requestAnimationFrame(step);
                }} else {{
                    window.setTimeout(() => {{ if (isRendering) globalControls.autoRotate = true; }}, 700);
                }}
            }};
            cameraTweenFrame = requestAnimationFrame(step);
        }}

        function seedModelPosters() {{
            const containers = Array.from(document.querySelectorAll('.webgl-container'));
            const reference = containers.find((container) => container.clientWidth && container.clientHeight);
            if (!reference) return;
            globalRenderer.setSize(reference.clientWidth, reference.clientHeight);
            globalCamera.aspect = reference.clientWidth / reference.clientHeight;
            globalCamera.updateProjectionMatrix();
            containers.forEach((container) => {{
                const posterMode = container.dataset.mode === 'thickness' ? 'thickness' : null;
                applyColorsToGeometry(globalMeshGroup.children[0].geometry, posterMode, false);
                globalRenderer.render(globalScene, globalCamera);
                try {{ container.style.backgroundImage = `url(${{globalRenderer.domElement.toDataURL('image/png')}})`; }} catch (error) {{}}
            }});
            applyColorsToGeometry(globalMeshGroup.children[0].geometry, null, false);
        }}

        function mountToContainer(container, mode) {{
            if (activeContainer === container) return;
            if (activeContainer) {{
                activeContainer.classList.remove('webgl-live');
            }}
            activeContainer = container;
            container.classList.add('webgl-live');
            container.appendChild(globalRenderer.domElement);

            const width = container.clientWidth;
            const height = container.clientHeight;
            globalRenderer.setSize(width, height);
            globalCamera.aspect = width / height;
            globalCamera.updateProjectionMatrix();

            // Adjust mode
            if (mode === 'null' || mode === null) {{
                applyColorsToGeometry(globalMeshGroup.children[0].geometry, null);
                globalControls.autoRotate = true;
                globalControls.autoRotateSpeed = 0.26;
                globalControls.enableZoom = true;
                globalControls.enablePan = true;
            }} else if (mode === 'thickness' || mode === 'draft') {{
                applyColorsToGeometry(globalMeshGroup.children[0].geometry, currentDefectMode || mode);
                globalControls.autoRotate = true;
                globalControls.autoRotateSpeed = 0.42;
                globalControls.enableZoom = true;
                globalControls.enablePan = true;
            }} else if (mode && mode.startsWith('minimap_')) {{
                const realMode = mode.split('_')[1];
                applyColorsToGeometry(globalMeshGroup.children[0].geometry, realMode);
                globalControls.autoRotate = true;
                globalControls.autoRotateSpeed = 0.72;
                globalControls.enableZoom = false; // Disable zoom for minimap
                globalControls.enablePan = false;

                // Reset camera closer for minimap
                let box = new THREE.Box3().setFromObject(globalMeshGroup);
                const maxDim = Math.max(box.max.x - box.min.x, box.max.y - box.min.y, box.max.z - box.min.z);
                globalCamera.position.set(maxDim*0.9, maxDim * 0.9, maxDim*0.9);
            }}
        }}

        function resizeActiveRenderer() {{
            if (!activeContainer || !globalRenderer || !globalCamera) return;
            const width = activeContainer.clientWidth;
            const height = activeContainer.clientHeight;
            if (!width || !height) return;
            globalRenderer.setSize(width, height);
            globalCamera.aspect = width / height;
            globalCamera.updateProjectionMatrix();
        }}

        window.switchDefectMode = function(mode, bringIntoView = false) {{
            currentDefectMode = mode;
            if (bringIntoView && window.innerWidth <= 760) {{
                userActivatedSummary = true;
                summaryActivatedAt = performance.now();
                document.getElementById('analysis-summary')?.scrollIntoView({{ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' }});
            }}
            document.querySelectorAll('.glass-btn').forEach(btn => btn.classList.remove('active'));
            const modeLabel = document.getElementById('activeModeLabel');
            if (mode === 'thickness') {{
                document.querySelectorAll('.glass-btn')[0].classList.add('active');
                if (modeLabel) modeLabel.textContent = 'WALL THICKNESS · DEFECT ISOLATION';
            }} else {{
                document.querySelectorAll('.glass-btn')[1].classList.add('active');
                if (modeLabel) modeLabel.textContent = 'DRAFT ANGLE · DEFECT ISOLATION';
            }}
            if (!globalMeshGroup) return;
            if (bringIntoView) {{
                const summaryContainer = document.querySelector('#analysis-summary .webgl-container');
                if (summaryContainer) mountToContainer(summaryContainer, mode);
            }}
            applyColorsToGeometry(globalMeshGroup.children[0].geometry, mode);
            tweenCameraForMode(mode);
        }};

        window.activateSummary3D = function(mode) {{
            userActivatedSummary = true;
            summaryActivatedAt = performance.now();
            const summary = document.getElementById('analysis-summary');
            const summaryContainer = summary?.querySelector('.webgl-container');
            if (window.innerWidth <= 760) summary?.scrollIntoView({{ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' }});
            if (globalMeshGroup && summaryContainer) mountToContainer(summaryContainer, mode);
            window.switchDefectMode(mode, false);
        }};

        // Hover Ghosting for Slide 3
        window.hoverGhostMode = function(mode, isHover) {{
            if (!globalMeshGroup) return;
            if (isHover) {{
                applyGhostColors(globalMeshGroup.children[0].geometry, mode);
            }} else {{
                applyColorsToGeometry(globalMeshGroup.children[0].geometry, null);
            }}
        }};

        function applyGhostColors(geometry, mode) {{
            const GHOST_SAFE = [0.15, 0.15, 0.15];
            const GHOST_DEFECT = [1.0, 0.1, 0.1];
            const colors = [];
            const threshold = mode === 'thickness' ? 1.2 : 1.0;

            for (let p of sceneData.primitives) {{
                let pId = p.primitive_id;
                let tris = p.triangles;
                if (!tris) continue;

                for (let t = 0; t < tris.length; t++) {{
                    let triVal = mode === 'thickness' ? thicknessMap[`${{pId}}-t${{t}}`] : undefined;
                    for (let vIdx of tris[t]) {{
                        let val = mode === 'thickness' ? triVal : (mode === 'draft' ? draftMap[`${{pId}}-v${{vIdx}}`] : undefined);
                        let color = GHOST_SAFE;
                        if (val !== undefined && val < threshold) {{
                            color = GHOST_DEFECT;
                        }}
                        colors.push(color[0], color[1], color[2]);
                    }}
                }}
            }}
            animateColorAttribute(geometry, colors, true);
        }}

        window.onload = () => {{
            const runStatAnimation = (slide) => {{
                slide.querySelectorAll('.stat-value').forEach((el) => {{
                    if (el.dataset.animated === 'true') return;
                    el.dataset.animated = 'true';
                    const target = Number(el.dataset.target);
                    if (!Number.isFinite(target) || reduceMotion) {{ el.textContent = el.dataset.target; return; }}
                    const startedAt = performance.now();
                    const duration = 720;
                    el.textContent = '0';
                    const tick = (now) => {{
                        const raw = Math.min(1, (now - startedAt) / duration);
                        const eased = 1 - Math.pow(1 - raw, 3);
                        el.textContent = String(Math.round(target * eased));
                        if (raw < 1) requestAnimationFrame(tick);
                    }};
                    requestAnimationFrame(tick);
                }});
            }};

            const summarySlide = document.querySelector('.summary-slide');
            if (summarySlide && 'IntersectionObserver' in window && !reduceMotion) {{
                const statsObserver = new IntersectionObserver((entries) => {{
                    if (entries.some((entry) => entry.isIntersecting)) {{
                        runStatAnimation(summarySlide);
                        statsObserver.disconnect();
                    }}
                }}, {{ threshold: 0.35 }});
                statsObserver.observe(summarySlide);
            }} else if (summarySlide) {{
                runStatAnimation(summarySlide);
            }}

            if (!sceneData.primitives) return;
            initGlobal3D();
            seedModelPosters();

            let hostSyncFrame = null;
            const syncRendererHost = () => {{
                hostSyncFrame = null;
                const containers = Array.from(document.querySelectorAll('.webgl-container'));
                const coverContainer = containers[0];
                const summaryContainer = containers.find((container) => container.dataset.mode === 'thickness');
                const coverSlideHeight = coverContainer?.closest('.slide')?.getBoundingClientRect().height || 0;
                const coverPriorityRange = Math.min(120, coverSlideHeight * 0.35);
                let target = null;
                let bestRatio = 0;

                if (window.scrollY <= 8 && performance.now() - summaryActivatedAt > 1200) userActivatedSummary = false;

                if (window.innerWidth <= 760 && summaryContainer) {{
                    const summaryRect = summaryContainer.getBoundingClientRect();
                    if (summaryRect.bottom > 0 && summaryRect.top < window.innerHeight) target = summaryContainer;
                }}

                if (!target && coverContainer && !userActivatedSummary && window.scrollY <= coverPriorityRange) {{
                    const coverRect = coverContainer.getBoundingClientRect();
                    if (coverRect.bottom > 0 && coverRect.top < window.innerHeight) target = coverContainer;
                }}

                if (!target) {{
                    containers.forEach((container) => {{
                        const rect = container.getBoundingClientRect();
                        const visiblePixels = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
                        const ratio = rect.height ? visiblePixels / rect.height : 0;
                        if (ratio > bestRatio + 0.01) {{ target = container; bestRatio = ratio; }}
                    }});
                }}

                isRendering = Boolean(target);
                if (target) mountToContainer(target, target.getAttribute('data-mode'));
            }};

            const requestHostSync = () => {{
                if (!hostSyncFrame) hostSyncFrame = requestAnimationFrame(syncRendererHost);
            }};

            syncRendererHost();
            window.addEventListener('scroll', requestHostSync, {{ passive: true }});
            window.addEventListener('resize', () => {{ resizeActiveRenderer(); requestHostSync(); }}, {{ passive: true }});
        }};
    </script>

    <!-- Human-readable traceability drawer -->
    <div id="traceBackdrop" class="trace-backdrop" onclick="closeTraceDrawer()">
        <aside class="trace-drawer" role="dialog" aria-modal="true" aria-labelledby="traceDrawerTitle" onclick="event.stopPropagation()">
            <div class="trace-drawer-head">
                <div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start;">
                    <div>
                        <div style="color:#7BD1D8; font-size:9px; font-weight:800; letter-spacing:.16em;">EVIDENCE CHAIN · 证据闭环</div>
                        <div id="traceDrawerTitle" style="margin-top:7px; font-size:18px; font-weight:800; line-height:1.3;"></div>
                        <div id="traceDrawerId" style="margin-top:7px; color:#AEB8C6; font:10px/1.3 Consolas,monospace;"></div>
                    </div>
                    <button id="traceCloseButton" onclick="closeTraceDrawer()" aria-label="关闭溯源面板" style="border:0; background:transparent; color:#FFFFFF; font-size:28px; cursor:pointer;">&times;</button>
                </div>
                <div id="traceMetricHero" style="margin-top:16px; padding:12px 14px; border-radius:9px; color:#FFFFFF; background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.12); font-size:14px; font-weight:800;"></div>
            </div>
            <div class="trace-drawer-body">
                <div style="margin-bottom:14px; color:#667085; font-size:12px; line-height:1.55;">用五个问题说明：为什么不合格、检测到了什么、问题在哪里、证据是什么，以及是否来自当前模型。</div>
                <div id="traceChain" class="trace-chain"></div>
                <details class="trace-tech-details">
                    <summary>技术详情（规则版本、算法与文件指纹）</summary>
                    <div id="traceTechGrid" class="trace-tech-grid"></div>
                </details>
                <button id="traceRawButton" onclick="openRawTraceFromDrawer()" style="width:100%; margin-top:12px; padding:11px 14px; border:1px solid #D0D5DD; border-radius:8px; color:#344054; background:#FFFFFF; font-weight:800; cursor:pointer;">查看原始 JSON 与几何记录</button>
            </div>
        </aside>
    </div>

    <script>
        let activeIssueId = null;
        let traceReturnFocus = null;

        function selectIssueFromSummary(issueId, bringIntoView = false) {{
            const item = ISSUE_UI_MAP[issueId];
            if (!item) return;
            activeIssueId = issueId;
            document.querySelectorAll('[data-issue-nav]').forEach((card) => {{
                card.classList.toggle('active', card.dataset.issueNav === issueId);
                card.querySelector('.issue-nav-select')?.setAttribute('aria-pressed', card.dataset.issueNav === issueId ? 'true' : 'false');
            }});
            if (bringIntoView && typeof window.activateSummary3D === 'function') window.activateSummary3D(item.mode);
            else if (typeof window.switchDefectMode === 'function') window.switchDefectMode(item.mode, false);
            const callout = document.getElementById('modelIssueCallout');
            if (callout) {{
                callout.querySelector('.callout-title').textContent = item.title;
                callout.querySelector('.callout-metric').textContent = `实测 ${{item.actual}} ${{item.unit}} · 要求 ${{getOperatorLabel(item.operator)}} ${{item.expected}} ${{item.unit}}`;
                callout.classList.add('visible');
            }}
            history.replaceState(null, '', `#selected-${{encodeURIComponent(issueId)}}`);
        }}

        function goToEvidence(issueId) {{
            selectIssueFromSummary(issueId);
            const target = document.getElementById(`issue-${{issueId}}`);
            if (!target) return;
            target.scrollIntoView({{ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' }});
            history.replaceState(null, '', `#issue-${{encodeURIComponent(issueId)}}`);
        }}

        function goToSummaryIssue(issueId) {{
            selectIssueFromSummary(issueId, true);
            const summary = document.getElementById('analysis-summary');
            if (summary) summary.scrollIntoView({{ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' }});
            history.replaceState(null, '', `#selected-${{encodeURIComponent(issueId)}}`);
        }}

        function returnToSummary() {{
            const summary = document.getElementById('analysis-summary');
            if (summary) summary.scrollIntoView({{ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' }});
            history.replaceState(null, '', activeIssueId ? `#selected-${{encodeURIComponent(activeIssueId)}}` : '#analysis-summary');
        }}

        function addTraceStep(label, value, actionLabel, actionHandler) {{
            const step = document.createElement('div');
            step.className = 'trace-step';
            const labelNode = document.createElement('div');
            labelNode.className = 'trace-step-label';
            labelNode.textContent = label;
            const valueNode = document.createElement('div');
            valueNode.className = 'trace-step-value';
            valueNode.textContent = value || '未提供';
            step.append(labelNode, valueNode);
            if (actionLabel && actionHandler) {{
                const action = document.createElement('button');
                action.className = 'trace-step-action';
                action.textContent = actionLabel;
                action.addEventListener('click', actionHandler);
                step.appendChild(action);
            }}
            document.getElementById('traceChain').appendChild(step);
        }}

        function addTechRow(label, value) {{
            const row = document.createElement('div');
            row.className = 'trace-tech-row';
            const labelNode = document.createElement('div');
            labelNode.className = 'trace-tech-label';
            labelNode.textContent = label;
            const valueNode = document.createElement('div');
            valueNode.className = 'trace-tech-value';
            valueNode.textContent = value || '未提供';
            row.append(labelNode, valueNode);
            document.getElementById('traceTechGrid').appendChild(row);
        }}

        function getMetricName(item) {{
            if (item.mode === 'thickness') return '最小壁厚';
            if (item.mode === 'draft') return '拔模角';
            return '检测值';
        }}

        function getOperatorLabel(operator) {{
            return {{'>=':'≥','<=':'≤','==':'='}}[operator] || operator || '';
        }}

        function getRequirementText(item) {{
            const metricName = getMetricName(item);
            const opText = {{'>=':'不得低于','>':'必须高于','<=':'不得高于','<':'必须低于','==':'应等于'}}[item.operator] || `应满足 ${{item.operator || '阈值'}}`;
            return `${{metricName}}${{opText}} ${{item.expected}} ${{item.unit}}。这是该问题被判定为不合格的直接标准。`;
        }}

        function getMeasurementText(item) {{
            const actual = Number(item.actual);
            const expected = Number(item.expected);
            let comparison = '未满足判定要求';
            if (Number.isFinite(actual) && Number.isFinite(expected)) {{
                const delta = Math.abs(actual - expected).toFixed(3).replace(/\\.?0+$/, '');
                if ((item.operator === '>=' || item.operator === '>') && actual < expected) comparison = `低于要求 ${{delta}} ${{item.unit}}`;
                else if ((item.operator === '<=' || item.operator === '<') && actual > expected) comparison = `高于要求 ${{delta}} ${{item.unit}}`;
                else comparison = `与判定阈值相差 ${{delta}} ${{item.unit}}`;
            }}
            return `${{getMetricName(item)}}实测为 ${{item.actual}} ${{item.unit}}，${{comparison}}。`;
        }}

        function openTraceDrawer(issueId) {{
            const item = ISSUE_UI_MAP[issueId];
            if (!item) return;
            activeIssueId = issueId;
            traceReturnFocus = document.activeElement;
            const raw = typeof RAW_TRACE_MAP !== 'undefined' ? RAW_TRACE_MAP[issueId] : null;
            document.getElementById('traceDrawerTitle').textContent = item.title;
            document.getElementById('traceDrawerId').textContent = `问题编号 · ${{item.id}}`;
            document.getElementById('traceMetricHero').textContent = `${{getMetricName(item)}}：${{item.actual}} ${{item.unit}}　｜　判定要求：${{getOperatorLabel(item.operator)}} ${{item.expected}} ${{item.unit}}`;
            const chain = document.getElementById('traceChain');
            chain.replaceChildren();
            const geometryCount = raw && raw.geometry_patches ? raw.geometry_patches.length : 0;
            const evidenceCount = (item.evidence || []).length;
            addTraceStep('为什么不合格？', getRequirementText(item));
            addTraceStep('实际检测到什么？', getMeasurementText(item));
            addTraceStep('问题位于模型哪里？', geometryCount
                ? `该检测结果关联 ${{geometryCount}} 组模型几何记录，风险区域已在 3D 模型中高亮。`
                : '风险区域已在 3D 模型中高亮；本次结果未提供可单独展开的底层几何记录。',
                '在 3D 中查看', () => {{ closeTraceDrawer(); goToSummaryIssue(issueId); }});
            addTraceStep('有哪些可查看的证据？', evidenceCount
                ? `已关联 ${{evidenceCount}} 张证据图，可前往对应问题页核对。`
                : '该问题未关联证据图片。',
                evidenceCount ? '查看证据页' : '', evidenceCount ? () => {{ closeTraceDrawer(); goToEvidence(issueId); }} : null);
            addTraceStep('是否来自当前模型？', item.input_sha256
                ? '是。报告保存了输入模型的文件指纹，可用于核验报告与源文件是否一致。'
                : '当前结果未提供输入模型指纹，无法进行文件一致性核验。');

            const techGrid = document.getElementById('traceTechGrid');
            techGrid.replaceChildren();
            addTechRow('问题代码', `${{item.code || '未提供'}} · ${{item.severity || '未分类'}}`);
            addTechRow('规则', `${{item.rule_id || '未提供'}} · 版本 ${{item.rule_version || '-'}} · 哈希 ${{item.rule_hash || '-'}}`);
            addTechRow('测量记录 ID', (item.measurement_ids || []).join(', ') || '未提供');
            addTechRow('分析算法', `${{item.algorithm_version || item.backend || '未提供'}}${{item.certified === undefined || item.certified === null ? '' : ` · certified=${{item.certified}}`}}`);
            addTechRow('几何记录', geometryCount ? `${{geometryCount}} 组关联记录` : '未提供');
            addTechRow('证据文件', (item.evidence || []).join(' · ') || '未提供');
            addTechRow('输入模型 SHA256', item.input_sha256 || '未提供');
            document.getElementById('traceRawButton').dataset.issueId = issueId;
            document.getElementById('traceBackdrop').classList.add('open');
            document.body.style.overflow = 'hidden';
            window.requestAnimationFrame(() => document.getElementById('traceCloseButton')?.focus());
        }}

        function closeTraceDrawer() {{
            const backdrop = document.getElementById('traceBackdrop');
            if (!backdrop.classList.contains('open')) return;
            backdrop.classList.remove('open');
            document.body.style.overflow = '';
            if (traceReturnFocus && typeof traceReturnFocus.focus === 'function') traceReturnFocus.focus();
            traceReturnFocus = null;
        }}

        function openRawTraceFromDrawer() {{
            const issueId = document.getElementById('traceRawButton').dataset.issueId;
            closeTraceDrawer();
            openRawTraceModal(issueId);
        }}

        document.addEventListener('keydown', (event) => {{
            if (event.key === 'Escape') closeTraceDrawer();
        }});

        window.addEventListener('load', () => {{
            const hash = decodeURIComponent(location.hash || '');
            if (hash.startsWith('#issue-')) {{
                const issueId = hash.slice(7);
                window.setTimeout(() => goToEvidence(issueId), 80);
            }} else if (hash.startsWith('#selected-')) {{
                const issueId = hash.slice(10);
                window.setTimeout(() => selectIssueFromSummary(issueId), 80);
            }}
        }});
    </script>

    <!-- Raw Data Traceability Modal -->
    <div id="rawTraceModal" role="dialog" aria-modal="true" aria-label="底层计算记录" onclick="closeRawTraceModal()" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:9999; backdrop-filter:blur(8px); align-items:center; justify-content:center;">
        <div onclick="event.stopPropagation()" style="background:#1E1E1E; width:min(1000px, 92vw); height:min(800px, 84vh); border-radius:12px; box-shadow:0 10px 40px rgba(0,0,0,0.5); display:flex; flex-direction:column; border:1px solid #333; overflow:hidden;">
            <!-- Header -->
            <div style="padding:15px 25px; background:#252526; border-bottom:1px solid #333; display:flex; justify-content:space-between; align-items:center;">
                <div style="display:flex; align-items:center; gap:10px;">
                    <span style="color:#E5E5E5; font-size:16px; font-weight:bold; font-family:Consolas, monospace;">&lt;/&gt; 底层计算原值溯源</span>
                    <span id="rawTraceEvalId" style="background:#007ACC; color:white; padding:2px 8px; border-radius:10px; font-size:12px; font-family:Consolas, monospace;"></span>
                </div>
                <button onclick="closeRawTraceModal()" aria-label="关闭底层计算记录" style="background:none; border:none; color:#858585; font-size:24px; cursor:pointer; padding:0; line-height:1;">&times;</button>
            </div>
            <!-- Body -->
            <div style="display:flex; flex:1; overflow:hidden;">
                <!-- Left: dfm_report.json block -->
                <div style="flex:1; border-right:1px solid #333; display:flex; flex-direction:column;">
                    <div style="padding:10px 15px; background:#2D2D2D; color:#CCCCCC; font-size:12px; font-family:Consolas, monospace; border-bottom:1px solid #333; text-transform:uppercase; letter-spacing:1px;">📄 dfm_report.json (规则计算原值)</div>
                    <pre id="rawTraceRule" style="margin:0; padding:15px; background:#1E1E1E; color:#D4D4D4; font-family:Consolas, monospace; font-size:13px; overflow:auto; flex:1; white-space:pre-wrap; word-break:break-all;"></pre>
                </div>
                <!-- Right: evidence_geometry.json block -->
                <div style="flex:1; display:flex; flex-direction:column;">
                    <div style="padding:10px 15px; background:#2D2D2D; color:#CCCCCC; font-size:12px; font-family:Consolas, monospace; border-bottom:1px solid #333; text-transform:uppercase; letter-spacing:1px;">🧊 evidence_geometry.json (3D 空间计算原值)</div>
                    <pre id="rawTraceGeo" style="margin:0; padding:15px; background:#1E1E1E; color:#D4D4D4; font-family:Consolas, monospace; font-size:13px; overflow:auto; flex:1; white-space:pre-wrap; word-break:break-all;"></pre>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Store raw trace data mapping injected from python
        const RAW_TRACE_MAP = {{raw_trace_json_placeholder}};

        function syntaxHighlight(json) {{
            if (typeof json != 'string') {{
                json = JSON.stringify(json, undefined, 2);
            }}
            json = json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            return json.replace(/("(\\\\u[a-zA-Z0-9]{{4}}|\\[^u]|[^\"])*"(\\s*:)?| (true|false|null) |-?\\d+(?:\\.\\d*)?(?:[eE][+\\-]?\\d+)?)/g, function (match) {{
                var cls = 'color: #B5CEA8;'; // number
                if (/^"/.test(match)) {{
                    if (/:$/.test(match)) {{
                        cls = 'color: #9CDCFE;'; // key
                    }} else {{
                        cls = 'color: #CE9178;'; // string
                    }}
                }} else if (/true|false/.test(match)) {{
                    cls = 'color: #569CD6;'; // boolean
                }} else if (/null/.test(match)) {{
                    cls = 'color: #569CD6;'; // null
                }}
                return '<span style="' + cls + '">' + match + '</span>';
            }});
        }}

        function openRawTraceModal(issueId) {{
            const data = RAW_TRACE_MAP[issueId];
            if (!data) return;

            document.getElementById('rawTraceEvalId').innerText = issueId;
            document.getElementById('rawTraceRule').innerHTML = syntaxHighlight(data.rule_evaluation);

            // Format geometry data nicely or show empty
            if (data.geometry_patches && data.geometry_patches.length > 0) {{
                document.getElementById('rawTraceGeo').innerHTML = syntaxHighlight(data.geometry_patches);
            }} else {{
                document.getElementById('rawTraceGeo').innerHTML = '<span style="color:#808080;">/* 该评估项未提供 3D 几何原值数据 */</span>';
            }}

            document.getElementById('rawTraceModal').style.display = 'flex';
            document.body.style.overflow = 'hidden';
        }}

        function closeRawTraceModal() {{
            document.getElementById('rawTraceModal').style.display = 'none';
            document.body.style.overflow = '';
        }}

        document.addEventListener('keydown', (event) => {{
            if (event.key === 'Escape' && document.getElementById('rawTraceModal').style.display === 'flex') closeRawTraceModal();
            if (event.key === 'Escape' && document.getElementById('noEvidenceModal').style.display === 'flex') closeNoEvidenceModal();
        }});
    </script>
    <script>
        // Leadership-demo navigation: one continuous story, with no runtime or
        // server dependency. PageUp/PageDown and Home/End mirror the controls.
        const demoSlides = Array.from(document.querySelectorAll('.slide'));
        const demoPrev = document.getElementById('demoPrev');
        const demoNext = document.getElementById('demoNext');
        const demoPageLabel = document.getElementById('demoPageLabel');
        const demoPageCount = document.getElementById('demoPageCount');
        const demoProgressBar = document.getElementById('demoProgressBar');
        const demoReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        let demoActiveIndex = 0;
        let demoScrollFrame = 0;

        function demoLabelFor(slide, index) {{
            if (slide.classList.contains('cover-slide')) return '报告封面';
            if (slide.classList.contains('summary-slide')) return '决策总览 · 3D 缺陷定位';
            if (slide.classList.contains('conclusion-slide')) return '综合评估与行动建议';
            const issueId = slide.dataset.issueId;
            const item = typeof ISSUE_UI_MAP !== 'undefined' ? ISSUE_UI_MAP[issueId] : null;
            return item ? `问题 ${{String(index - 1).padStart(2, '0')}} · ${{item.title}}` : `证据页 ${{String(index + 1).padStart(2, '0')}}`;
        }}

        function demoUpdateChrome(index) {{
            demoActiveIndex = Math.max(0, Math.min(index, demoSlides.length - 1));
            const slide = demoSlides[demoActiveIndex];
            demoPageLabel.textContent = demoLabelFor(slide, demoActiveIndex);
            demoPageCount.textContent = `${{String(demoActiveIndex + 1).padStart(2, '0')}} / ${{String(demoSlides.length).padStart(2, '0')}}`;
            demoProgressBar.style.width = `${{((demoActiveIndex + 1) / Math.max(demoSlides.length, 1)) * 100}}%`;
            demoPrev.disabled = demoActiveIndex === 0;
            demoNext.disabled = demoActiveIndex === demoSlides.length - 1;
        }}

        function demoGoTo(index) {{
            const targetIndex = Math.max(0, Math.min(index, demoSlides.length - 1));
            demoSlides[targetIndex]?.scrollIntoView({{
                behavior: demoReducedMotion ? 'auto' : 'smooth',
                block: 'center',
            }});
            demoUpdateChrome(targetIndex);
        }}

        function demoDetectActiveSlide() {{
            demoScrollFrame = 0;
            const viewportCenter = window.innerHeight / 2;
            let closestIndex = 0;
            let closestDistance = Number.POSITIVE_INFINITY;
            demoSlides.forEach((slide, index) => {{
                const rect = slide.getBoundingClientRect();
                const distance = Math.abs((rect.top + rect.bottom) / 2 - viewportCenter);
                if (distance < closestDistance) {{
                    closestDistance = distance;
                    closestIndex = index;
                }}
            }});
            if (closestIndex !== demoActiveIndex) demoUpdateChrome(closestIndex);
        }}

        function demoOverlayIsOpen() {{
            return document.getElementById('traceBackdrop')?.classList.contains('open')
                || document.getElementById('rawTraceModal')?.style.display === 'flex'
                || document.getElementById('noEvidenceModal')?.style.display === 'flex'
                || document.getElementById('galleryOverlay')?.style.display === 'flex';
        }}

        demoPrev.addEventListener('click', () => demoGoTo(demoActiveIndex - 1));
        demoNext.addEventListener('click', () => demoGoTo(demoActiveIndex + 1));
        window.addEventListener('scroll', () => {{
            if (!demoScrollFrame) demoScrollFrame = window.requestAnimationFrame(demoDetectActiveSlide);
        }}, {{ passive: true }});
        window.addEventListener('resize', demoDetectActiveSlide, {{ passive: true }});
        document.addEventListener('keydown', (event) => {{
            if (demoOverlayIsOpen()) return;
            const tagName = event.target?.tagName;
            if (tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT') return;
            if (event.key === 'PageDown') {{ event.preventDefault(); demoGoTo(demoActiveIndex + 1); }}
            if (event.key === 'PageUp') {{ event.preventDefault(); demoGoTo(demoActiveIndex - 1); }}
            if (event.key === 'Home') {{ event.preventDefault(); demoGoTo(0); }}
            if (event.key === 'End') {{ event.preventDefault(); demoGoTo(demoSlides.length - 1); }}
        }});
        demoUpdateChrome(0);
        window.addEventListener('load', demoDetectActiveSlide);
    </script>
</body>
</html>

"""

    # Inject raw trace map
    html = html.replace('{raw_trace_json_placeholder}', json.dumps(raw_trace_map, ensure_ascii=False))
    with open(output_html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"Generated complete 3D embedded report at: {output_html_path}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Generate the original leadership-demo V6 HTML from two JSONL contracts."
    )
    parser.add_argument("llm_content", type=Path, help="LLM-organized content contract JSONL.")
    parser.add_argument("runtime_data", type=Path, help="Deterministic runtime/resource contract JSONL.")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output HTML path.")
    parser.add_argument(
        "--vendor-dir",
        type=Path,
        default=DEFAULT_VENDOR_DIR,
        help="Directory containing the pinned Three.js and OrbitControls files.",
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    generate_html(
        args.llm_content.resolve(),
        args.runtime_data.resolve(),
        args.output.resolve(),
        args.vendor_dir.resolve(),
    )
