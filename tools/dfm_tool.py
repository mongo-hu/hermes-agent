"""Thin Hermes registration adapter for the built-in DFM capability."""

import json

from tools.dfm.errors import DFMError
from tools.dfm.service import get_dfm_service
from tools.registry import registry


def _call(kind: str, args: dict, **context) -> str | dict:
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
        if kind == "analysis" and args.get("action") in {"start", "render_html"}:
            params["_tool_progress_callback"] = context.get("tool_progress_callback")
            params["_tool_call_id"] = context.get("tool_call_id")
        if kind == "analysis" and args.get("action") == "discover":
            # DFM drawing vision is an internal side-call, but it must use the
            # exact provider/model/credentials of the active main Agent.  Keep
            # this snapshot out of model-visible arguments and persisted state.
            from agent.auxiliary_client import get_runtime_main

            params["_main_runtime"] = get_runtime_main()
        result = (
            service.project(args.get("action", ""), **params)
            if kind == "project"
            else service.analysis(args.get("action", ""), **params)
        )
        if isinstance(result, dict) and result.get("_multimodal") is True:
            return result
        return json.dumps(result, ensure_ascii=False)
    except DFMError as exc:
        return json.dumps(exc.to_dict(), ensure_ascii=False)


DFM_PROJECT_SCHEMA = {
    "name": "dfm_project",
    "description": "Manage durable DFM projects and register inputs. During project intake, call add_input once for every attached STEP/STP, Parasolid x_t, PDF, PNG, JPG, or JPEG file before status, discover, or plan; never silently omit a drawing when CAD is also attached. STEP registration may also produce an OCCT 3D preview when dfm-geometry is available. Use status before analysis to inspect format and process capabilities. confirm_fact may be called only after the user explicitly answers a clarification; never infer engineering facts from geometry.",
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
    "description": "Run the DFM workflow. discover performs drawing crop planning and fact extraction internally with the active main Hermes model and credentials; do not open PDFs in a browser or call vision_analyze/drawing_context. Use fusion_context and submit_fusion_links for semantic proposals checked against geometry IDs. An HTML-capable STEP run remains reporting until the current Hermes model authors dfm-html-llm/v1; render_html queues background rendering and returns immediately, so wait for succeeded status or its completion notification before result. Never configure a second model endpoint or reinterpret the drawing during reporting.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover",
                    "drawing_context",
                    "submit_crop_plan",
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
                "description": "For action=report_context, wait up to this many seconds for deterministic analysis to reach report editing. The returned Runtime is inline; never use terminal/read-file to obtain it.",
            },
            "input_id": {
                "type": "string",
                "description": "Drawing input identity returned by discover/drawing_context.",
            },
            "expected_revision": {
                "type": "integer",
                "minimum": 0,
                "description": "Manifest revision returned by drawing_context, submit_crop_plan, or fusion_context; required by semantic submissions.",
            },
            "crop_regions": {
                "type": "array",
                "maxItems": 48,
                "description": "Complete semantic crop plan produced by the current Hermes model from all drawing page images.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "page": {"type": "integer", "minimum": 1},
                        "region_id": {"type": "string", "minLength": 1, "maxLength": 80},
                        "type": {
                            "type": "string",
                            "enum": ["notes", "title_block", "materials_bom", "assembly_dimensions", "manufacturing_callouts", "other"],
                        },
                        "bbox_1000": {
                            "type": "array", "minItems": 4, "maxItems": 4,
                            "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                        },
                        "decision": {"type": "string", "enum": ["keep", "keep_uncertain"]},
                        "reason": {"type": "string", "maxLength": 1000},
                    },
                    "required": ["page", "region_id", "type", "bbox_1000", "decision"],
                },
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
                        "source_region_refs": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "source_text": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                    "required": ["kind", "value", "confidence", "source_region_refs"],
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
                "description": "Current-Agent final editorial report for action=render_html. Summarize, organize, and localize persisted drawing observations when present plus deterministic report facts into the user's language; preserve IDs, codes, numbers, operators, units, and versions exactly. Do not reinterpret the drawing, invent engineering facts, claim unevaluated checks passed, or mechanically copy raw Runtime prose as the final report.",
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
