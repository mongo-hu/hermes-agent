"""Thin Hermes registration adapter for the built-in DFM capability."""

import json
from collections import Counter

from tools.dfm.errors import DFMError
from tools.dfm.service import get_dfm_service
from tools.registry import registry


_session_projects: dict[str, str] = {}
_TOOL_PREVIEW_LIMIT = 50


def _project_status_summary(result: dict) -> dict:
    project = result["project"]
    snapshots = project.get("discovery_snapshots", [])
    inputs = project.get("inputs", [])
    facts = project.get("facts", [])
    plans = project.get("plans", [])
    runs = project.get("runs", [])
    return {
        "ok": True,
        "project_id": project["project_id"],
        "project": {
            key: project[key]
            for key in ("project_id", "name", "process", "input_mode", "revision")
            if key in project
        } | {
            "inputs": [
                {key: item[key] for key in ("input_id", "kind", "source_name", "sha256") if key in item}
                for item in inputs[-_TOOL_PREVIEW_LIMIT:]
            ],
            "input_count": len(inputs),
            "inputs_omitted": max(0, len(inputs) - _TOOL_PREVIEW_LIMIT),
            "facts": [
                {key: item[key] for key in ("name", "value", "status") if key in item}
                for item in facts[-_TOOL_PREVIEW_LIMIT:]
            ],
            "fact_count": len(facts),
            "facts_omitted": max(0, len(facts) - _TOOL_PREVIEW_LIMIT),
            "open_clarifications": project.get("open_clarifications", []),
            "discovery_snapshot_id": snapshots[-1]["snapshot_id"] if snapshots else None,
            "plan_count": len(plans),
            "latest_plan": next(
                (
                    {key: item[key] for key in ("plan_id", "phase", "status") if key in item}
                    for item in reversed(plans)
                ),
                None,
            ),
            "run_count": len(runs),
            "finding_count": len(project.get("findings", [])),
            "artifact_count": len(project.get("artifacts", [])),
            "latest_run": next(
                (
                    {key: item[key] for key in ("run_id", "status", "plan_id") if key in item}
                    for item in reversed(runs)
                ),
                None,
            ),
        },
        "ontology": result.get("ontology"),
        "capabilities": {
            key: {field: item[field] for field in ("status", "reason", "error_code") if field in item}
            for key, item in result.get("capabilities", {}).items()
        },
        "process_capabilities": {
            key: {field: item[field] for field in ("status", "reason", "error_code") if field in item}
            for key, item in result.get("process_capabilities", {}).items()
        },
    }


def _discovery_tool_summary(result: dict) -> dict:
    """Expose discovery coverage without repeating persisted geometry details."""

    features = result["features"]
    regions = result["regions"]
    snapshot = result["snapshot"]
    plan = result["plan"]
    capability = result["capability"]
    return {
        "ok": True,
        "project_id": result["project_id"],
        "phase": "discovery",
        "plan": {
            key: plan[key]
            for key in ("plan_id", "status", "phase", "scope_id", "scope_version")
            if key in plan
        },
        "snapshot": {
            key: snapshot[key]
            for key in ("snapshot_id", "status", "content_sha256", "provider_versions")
            if key in snapshot
        },
        "feature_count": len(features),
        "feature_counts": dict(Counter(item["kind"] for item in features)),
        "features": [
            {key: item[key] for key in ("feature_id", "kind", "status") if key in item}
            for item in features[:_TOOL_PREVIEW_LIMIT]
        ],
        "features_omitted": max(0, len(features) - _TOOL_PREVIEW_LIMIT),
        "region_count": len(regions),
        "region_counts": dict(
            Counter(item.get("role") or item.get("semantic_label") or "unknown" for item in regions)
        ),
        "regions": [
            {key: item[key] for key in ("region_id", "role", "semantic_label") if key in item}
            for item in regions[:_TOOL_PREVIEW_LIMIT]
        ],
        "regions_omitted": max(0, len(regions) - _TOOL_PREVIEW_LIMIT),
        "observation_status_counts": dict(
            Counter(item["status"] for item in result["observations"])
        ),
        "fusion_link_status_counts": dict(
            Counter(item["status"] for item in result["fusion_links"])
        ),
        "capability": {
            key: capability[key] for key in ("status", "providers") if key in capability
        },
        "drawing_discovery": {
            "status": result["drawing_discovery"].get("status")
        },
        "open_clarifications": result["open_clarifications"],
        "next_action": "clarify" if result["open_clarifications"] else "plan",
    }


def _plan_tool_summary(result: dict) -> dict:
    plan = result["plan"]
    bindings = plan["rule_bindings"]
    check_ids = sorted({item["check_id"] for item in bindings})
    rule_ids = sorted({item["rule_id"] for item in bindings})
    capability = result["capability"]
    return {
        "ok": True,
        "project_id": result["project_id"],
        "plan": {
            key: plan[key]
            for key in (
                "plan_id", "phase", "status", "process", "scope_id", "scope_version",
                "ontology_snapshot_id", "discovery_snapshot_refs", "analyzer_keys",
                "input_hashes",
            )
            if key in plan
        } | {
            "operation_count": len(plan["operations"]),
            "rule_binding_count": len(bindings),
            "check_ids": check_ids[:_TOOL_PREVIEW_LIMIT],
            "check_ids_omitted": max(0, len(check_ids) - _TOOL_PREVIEW_LIMIT),
            "rule_ids": rule_ids[:_TOOL_PREVIEW_LIMIT],
            "rule_ids_omitted": max(0, len(rule_ids) - _TOOL_PREVIEW_LIMIT),
        },
        "capability": {
            key: capability[key]
            for key in ("analyzer_key", "status", "reason", "error_code")
            if key in capability
        },
        "next_action": (
            "start"
            if plan["status"] == "ready" and capability["status"] == "available"
            else "stop"
        ),
    }


def _run_tool_summary(result: dict, action: str) -> dict:
    run = result["run"]
    status = run["status"]
    if status == "succeeded":
        next_action = "complete" if action == "result" else "result"
    elif status in {"failed", "blocked", "cancelled"}:
        next_action = "complete"
    elif run.get("analyzer_key") in {"occt_cpp", "step"}:
        next_action = "report_context"
    else:
        next_action = "status"
    return {
        "ok": True,
        "project_id": result["project_id"],
        "run": {
            key: run[key]
            for key in (
                "run_id", "plan_id", "analyzer_key", "status", "stage",
                "progress_percent", "error",
            )
            if key in run
        } | {
            "artifacts": [
                {key: item[key] for key in ("artifact_id", "kind", "path") if key in item}
                for item in run.get("artifacts", [])
            ],
            "diagnostics": run.get("diagnostics", {}),
        },
        "next_action": next_action,
        **({"wait_seconds": 60} if next_action == "report_context" else {}),
    }


def active_dfm_project_id(session_id: str | None) -> str | None:
    """Return the project used by this conversation's DFM tool calls."""
    return _session_projects.get(session_id) if session_id else None


def _call(kind: str, args: dict, **context) -> str:
    try:
        if kind == "project" and args.get("action") == "confirm_fact":
            raise DFMError(
                "user_action_required",
                "Use the interactive DFM clarification flow; only its user response can confirm a DFM fact.",
            )
        if kind == "analysis" and args.get("action") == "cancel":
            raise DFMError(
                "user_action_required",
                "DFM cancellation is reserved for an explicit user-interface action; the Agent cannot cancel a run.",
            )
        service = get_dfm_service()
        params = {key: value for key, value in args.items() if key != "action"}
        if kind == "project" and args.get("action") == "add_input":
            from tools.terminal_tool import resolve_task_overrides

            working_dir = resolve_task_overrides(context.get("task_id")).get("cwd")
            if working_dir:
                params["working_dir"] = working_dir
        if kind == "analysis" and args.get("action") in {"start", "render_html"}:
            params["_tool_progress_callback"] = context.get("tool_progress_callback")
            params["_tool_call_id"] = context.get("tool_call_id")
        result = (
            service.project(args.get("action", ""), **params)
            if kind == "project"
            else service.analysis(args.get("action", ""), **params)
        )
        session_id = context.get("session_id")
        project_id = (
            result.get("project_id") or args.get("project_id")
            if isinstance(result, dict) and result.get("ok") is True else None
        )
        if isinstance(session_id, str) and isinstance(project_id, str):
            _session_projects[session_id] = project_id
        if kind == "project" and args.get("action") == "status" and isinstance(result, dict) and "project" in result:
            result = _project_status_summary(result)
        if kind == "analysis" and isinstance(result, dict):
            action = args.get("action")
            if action == "discover" and "snapshot" in result:
                result = _discovery_tool_summary(result)
            elif action == "plan" and "plan" in result:
                result = _plan_tool_summary(result)
            elif action in {"start", "status", "result"} and "run" in result:
                result = _run_tool_summary(result, action)
        return json.dumps(result, ensure_ascii=False)
    except DFMError as exc:
        return json.dumps(exc.to_dict(), ensure_ascii=False)


DFM_PROJECT_SCHEMA = {
    "name": "dfm_project",
    "description": "Manage durable DFM projects and the installed workspace ontology. Use sync_ontology only when the user asks. Create projects without inferring a process. Every open DFM clarification must be answered through the interactive DFM clarification flow with dfm_project_id and dfm_fact_name; it fetches all published options and confirms the user's answer atomically. The Agent cannot call confirm_fact directly. Register STEP, Parasolid x_t, or drawing inputs. Status returns a bounded project summary; do not use it to poll an active analysis run. Never infer engineering facts from geometry.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "add_input",
                    "status",
                    "list",
                    "ontology_status",
                    "sync_ontology",
                ],
            },
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "path": {
                "type": "string",
                "description": "Local path or Desktop @file: reference",
            },
            "idempotency_key": {"type": "string"},
        },
        "required": ["action"],
    },
}

DFM_ANALYSIS_SCHEMA = {
    "name": "dfm_analysis",
    "description": "Run the DFM workflow. Discovery, plan, start, status, and result return bounded control summaries; follow next_action and use feature_counts for exact discovery counts. Features and regions are previews with explicit omitted counts. Drawing OCR is deterministic; use drawing_context and the current Hermes model once to organize every explicit drawing fact into validated drawing observations. Use fusion_context and submit_fusion_links for Agent semantic proposals that the service checks against geometry IDs. An HTML-capable STEP run (PDF drawing optional) remains reporting (not succeeded) after deterministic analysis; call report_context to obtain the complete Runtime and required_issue_ids, then author dfm-html-llm/v1 with exactly those issue IDs and call render_html. render_html validates IDs before queuing background rendering; wait for succeeded status or its completion notification before calling result. Only a validated report.html completes the run. Do not reinterpret OCR during reporting. The external OCCT C++ analyzer is integrated as experimental; PythonOCC remains the reference STEP backend and NX/Parasolid remains optional. Unavailable analyzers fail explicitly; never infer engineering findings from that status.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover",
                    "drawing_context",
                    "submit_observations",
                    "fusion_context",
                    "submit_fusion_links",
                    "plan",
                    "start",
                    "status",
                    "report_context",
                    "result",
                    "render_html",
                    "context",
                ],
            },
            "project_id": {"type": "string"},
            "plan_id": {
                "type": "string",
                "description": "Plan ID returned by action=plan. Required when action=start; always pass that exact ID and never infer a different plan.",
            },
            "run_id": {
                "type": "string",
                "description": "Run ID returned by start. Always pass it to status or result; if omitted, the service can infer it only when unambiguous.",
            },
            "wait_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 60,
                "description": "For action=report_context, wait up to this many seconds for deterministic analysis to reach report editing. The complete Runtime and exact required_issue_ids are inline; never use terminal/read-file to obtain them.",
            },
            "input_id": {
                "type": "string",
                "description": "Drawing input identity returned by discover/drawing_context.",
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional drawing page filter for bounded OCR context.",
            },
            "expected_revision": {
                "type": "integer",
                "minimum": 0,
                "description": "Manifest revision returned by drawing_context or fusion_context; required by semantic submissions.",
            },
            "observations": {
                "type": "array",
                "maxItems": 200,
                "description": "Agent-interpreted explicit drawing facts. The service creates IDs and status.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "pattern": "^[a-z][a-z0-9_.-]{0,99}$",
                        },
                        "value": {},
                        "unit": {"type": ["string", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "source_fragment_refs": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["kind", "value", "confidence", "source_fragment_refs"],
                },
            },
            "fusion_links": {
                "type": "array",
                "maxItems": 200,
                "description": "Agent semantic target proposals. The service derives IDs/status and validates geometry relationships.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "observation_refs": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "feature_refs": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "region_refs": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string", "maxLength": 1000},
                    },
                    "required": [
                        "observation_refs",
                        "feature_refs",
                        "region_refs",
                        "confidence",
                    ],
                },
            },
            "check_id": {
                "type": "string",
                "description": "Stable Check identity required when action=context, keeping the ontology/rule response bounded.",
            },
            "base_plan_id": {
                "type": "string",
                "description": "Invalidated plan to rebuild with only affected operations.",
            },
            "analyzer_key": {
                "type": "string",
                "enum": [
                    "occt_cpp_external",
                    "pythonocc_internal",
                    "parasolid",
                    "drawing",
                    "fusion",
                ],
                "description": "Geometry execution path. Use occt_cpp_external for dfm-geometry.exe or pythonocc_internal for the Agent-owned PythonOCC worker.",
            },
            "idempotency_key": {"type": "string"},
            "llm_content": {
                "type": "object",
                "additionalProperties": False,
                "description": "Current-Agent final editorial report for action=render_html. Summarize, organize, and localize persisted drawing observations when present plus deterministic report facts into the user's language; preserve IDs, codes, numbers, operators, units, and versions exactly. Do not reinterpret OCR, invent engineering facts, claim unevaluated checks passed, or mechanically copy raw Runtime prose as the final report.",
                "properties": {
                    "schema_version": {
                        "type": "string",
                        "const": "dfm-html-llm/v1",
                    },
                    "part": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "general_tolerance": {"type": ["string", "null"]},
                            "technical_note": {"type": ["string", "null"]},
                        },
                        "required": [
                            "name",
                            "general_tolerance",
                            "technical_note",
                        ],
                    },
                    "issues": {
                        "type": "array",
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "issue_id": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Copy exactly one ID from report_context.required_issue_ids; provide every required ID once and no other IDs.",
                                },
                                "title": {"type": "string", "minLength": 1},
                                "description": {"type": "string", "minLength": 1},
                            },
                            "required": ["issue_id", "title", "description"],
                        },
                    },
                    "conclusion": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "assessment_level": {"type": "string", "minLength": 1},
                            "summary": {"type": "string", "minLength": 1},
                            "risks": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"$ref": "#/$defs/report_copy_item"},
                            },
                            "actions": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"$ref": "#/$defs/report_copy_item"},
                            },
                        },
                        "required": [
                            "assessment_level",
                            "summary",
                            "risks",
                            "actions",
                        ],
                    },
                },
                "required": ["schema_version", "part", "issues", "conclusion"],
            },
        },
        "$defs": {
            "report_copy_item": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                },
                "required": ["title", "description"],
            }
        },
        "required": ["action", "project_id"],
    },
}

registry.register(
    name="dfm_project",
    toolset="dfm",
    schema=DFM_PROJECT_SCHEMA,
    handler=lambda args, **kwargs: _call("project", args, **kwargs),
    emoji="🏭",
)
registry.register(
    name="dfm_analysis",
    toolset="dfm",
    schema=DFM_ANALYSIS_SCHEMA,
    handler=lambda args, **kwargs: _call("analysis", args, **kwargs),
    emoji="📐",
)
