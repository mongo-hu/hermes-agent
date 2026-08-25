"""Thin Hermes registration adapter for the built-in DFM capability."""

import json

from tools.dfm.errors import DFMError
from tools.dfm.service import get_dfm_service
from tools.registry import registry


def _call(kind: str, args: dict, **context) -> str:
    try:
        service = get_dfm_service()
        params = {key: value for key, value in args.items() if key != "action"}
        if kind == "project" and args.get("action") == "add_input":
            from tools.terminal_tool import resolve_task_overrides

            working_dir = resolve_task_overrides(context.get("task_id")).get("cwd")
            if working_dir:
                params["working_dir"] = working_dir
        if kind == "analysis" and args.get("action") == "start":
            params["_tool_progress_callback"] = context.get("tool_progress_callback")
            params["_tool_call_id"] = context.get("tool_call_id")
        result = service.project(args.get("action", ""), **params) if kind == "project" else service.analysis(args.get("action", ""), **params)
        return json.dumps(result, ensure_ascii=False)
    except DFMError as exc:
        return json.dumps(exc.to_dict(), ensure_ascii=False)


DFM_PROJECT_SCHEMA = {
    "name": "dfm_project",
    "description": "Manage durable DFM projects and register STEP/STP or drawing inputs. Native OCCT injection analysis is experimental and requires explicit verification_level=experimental. Use status before analysis to inspect capabilities. confirm_fact may be called only after the user explicitly answers a clarification; never infer engineering facts from geometry.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["create", "add_input", "status", "confirm_fact", "list"]},
        "project_id": {"type": "string"}, "name": {"type": "string"},
        "path": {"type": "string", "description": "Local path or Desktop @file: reference"},
        "fact_name": {"type": "string", "description": "Canonical names: material, model_units, pull_dir. Use only the user's explicit answer."}, "fact_value": {"description": "The user's explicit answer; never a model-inferred value."}, "idempotency_key": {"type": "string"},
    }, "required": ["action"]},
}

DFM_ANALYSIS_SCHEMA = {
    "name": "dfm_analysis",
    "description": "Discover, plan, and manage non-blocking DFM runs. Objective STEP geometry is executed by the external Analysis Situs/OCCT engine at experimental verification level; certified execution never silently downgrades. Hermes owns ontology resolution, evaluation, evidence, findings, and reporting.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["discover", "plan", "start", "status", "cancel", "result", "context"]},
        "project_id": {"type": "string"}, "plan_id": {"type": "string"}, "run_id": {"type": "string", "description": "Run ID returned by start. Always pass it to status, result, or cancel; if omitted, the service can infer it only when unambiguous."},
        "check_id": {"type": "string", "description": "Stable Check identity required when action=context, keeping the ontology/rule response bounded."},
        "base_plan_id": {"type": "string", "description": "Invalidated plan to rebuild with only affected operations."},
        "process": {"type": "string", "enum": ["injection", "die_casting"], "description": "Manufacturing process selected by the user. Omit only to keep the project's current/default process."},
        "analyzer_key": {"type": "string", "enum": ["occt", "drawing", "fusion"]},
        "verification_level": {"type": "string", "enum": ["certified", "experimental"], "description": "OCCT v0.2.0 requires an explicit experimental selection; certified never silently downgrades."},
        "confirm_cancel": {"type": "boolean", "description": "Set true only when the user explicitly requested cancellation. Never set merely because progress is unchanged; wait for the configured timeout."},
        "idempotency_key": {"type": "string"},
    }, "required": ["action", "project_id"]},
}

registry.register(name="dfm_project", toolset="dfm", schema=DFM_PROJECT_SCHEMA, handler=lambda args, **kwargs: _call("project", args, **kwargs), emoji="🏭")
registry.register(name="dfm_analysis", toolset="dfm", schema=DFM_ANALYSIS_SCHEMA, handler=lambda args, **kwargs: _call("analysis", args, **kwargs), emoji="📐")
