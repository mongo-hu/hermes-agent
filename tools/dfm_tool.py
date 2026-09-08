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
    "description": "Run the DFM workflow. Drawing OCR is deterministic; use drawing_context and the current Hermes model once to organize every explicit drawing fact into validated drawing observations. Use fusion_context and submit_fusion_links for Agent semantic proposals that the service checks against geometry IDs. A STEP+PDF run remains reporting (not succeeded) after deterministic analysis; call report_context to obtain the complete Runtime, then author dfm-html-llm/v1 and call render_html. Only a validated report.html completes the run. Do not reinterpret OCR during reporting. The external OCCT C++ analyzer is integrated as experimental; PythonOCC remains the reference STEP backend and NX/Parasolid remains optional. Unavailable analyzers fail explicitly; never infer engineering findings from that status.",
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
            "plan_id": {"type": "string"},
            "run_id": {
                "type": "string",
                "description": "Run ID returned by start. Always pass it to status or result; if omitted, the service can infer it only when unambiguous.",
            },
            "wait_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 60,
                "description": "For action=report_context, wait up to this many seconds for deterministic analysis to reach report editing. The returned Runtime is inline; never use terminal/read-file to obtain it.",
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
            "llm_content": {
                "type": "object",
                "additionalProperties": False,
                "description": "Current-Agent final editorial report for action=render_html. Summarize, organize, and localize persisted drawing observations plus deterministic report facts into the user's language; preserve IDs, codes, numbers, operators, units, and versions exactly. Do not reinterpret OCR, invent engineering facts, claim unevaluated checks passed, or mechanically copy raw Runtime prose as the final report.",
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
                                "issue_id": {"type": "string", "minLength": 1},
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
