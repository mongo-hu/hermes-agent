"""Thin Hermes registration adapter for the built-in DFM capability."""

import json

from tools.dfm.errors import DFMError
from tools.dfm.service import get_dfm_service
from tools.registry import registry


def _call(kind: str, args: dict, **context) -> str:
    try:
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
        if kind == "analysis" and args.get("action") == "start":
            params["_tool_progress_callback"] = context.get("tool_progress_callback")
            params["_tool_call_id"] = context.get("tool_call_id")
        result = (
            service.project(args.get("action", ""), **params)
            if kind == "project"
            else service.analysis(args.get("action", ""), **params)
        )
        return json.dumps(result, ensure_ascii=False)
    except DFMError as exc:
        return json.dumps(exc.to_dict(), ensure_ascii=False)


DFM_PROJECT_SCHEMA = {
    "name": "dfm_project",
    "description": "Manage durable DFM projects and register STEP, Parasolid x_t, or drawing inputs. STEP registration may also produce an OCCT 3D preview when dfm-geometry is available. Use status before analysis to inspect format and process capabilities. confirm_fact may be called only after the user explicitly answers a clarification; never infer engineering facts from geometry.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "add_input", "status", "confirm_fact", "list"],
            },
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "process": {
                "type": "string",
                "enum": ["injection", "die_casting"],
                "description": "Manufacturing intent selected by the user when creating the project.",
            },
            "path": {
                "type": "string",
                "description": "Local path or Desktop @file: reference",
            },
            "fact_name": {
                "type": "string",
                "description": "Canonical names: material, model_units, pull_dir. Use only the user's explicit answer.",
            },
            "fact_value": {
                "description": "The user's explicit answer; never a model-inferred value."
            },
            "idempotency_key": {"type": "string"},
        },
        "required": ["action"],
    },
}

DFM_ANALYSIS_SCHEMA = {
    "name": "dfm_analysis",
    "description": "Run the DFM workflow. Drawing OCR is deterministic; use drawing_context and the current Hermes model to interpret explicit facts, then submit_observations for validated persistence. Use fusion_context and submit_fusion_links for Agent semantic proposals that the service checks against geometry IDs. The external OCCT C++ analyzer is integrated as experimental; PythonOCC remains the reference STEP backend and NX/Parasolid remains optional. Unavailable analyzers fail explicitly; never infer engineering findings from that status.",
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
                    "result",
                    "context",
                ],
            },
            "project_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "run_id": {
                "type": "string",
                "description": "Run ID returned by start. Always pass it to status or result; if omitted, the service can infer it only when unambiguous.",
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
            "process": {
                "type": "string",
                "enum": ["injection", "die_casting"],
                "description": "Manufacturing process selected by the user. Pass it to discover before compiling the analysis plan.",
            },
            "analyzer_key": {
                "type": "string",
                "enum": ["occt_cpp", "step", "parasolid", "drawing", "fusion"],
            },
            "idempotency_key": {"type": "string"},
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
