#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
    dfm_project_id: Optional[str] = None,
    dfm_fact_name: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner (cli.py / gateway).
        dfm_project_id: Optional project whose open factor is being answered.
        dfm_fact_name: Factor reference from the open DFM clarification
                       (name or name@check_id); options come from ontology.

    Returns:
        JSON string with the user's response.
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    dfm_service = None
    if not dfm_project_id and not dfm_fact_name and session_id:
        from tools.dfm.errors import DFMError
        from tools.dfm.service import get_dfm_service
        from tools.dfm_tool import active_dfm_project_id

        active_project_id = active_dfm_project_id(session_id)
        if active_project_id:
            try:
                active_project = get_dfm_service().project(
                    "status", project_id=active_project_id
                )["project"]
            except DFMError:
                active_project = None
            if active_project is not None:
                open_facts = active_project["open_clarifications"]
                if open_facts:
                    # A DFM answer must be attributed to a specific project
                    # and factor before any UI question is shown.
                    return json.dumps({
                        "error": "DFM clarification requires dfm_project_id and dfm_fact_name.",
                        "code": "dfm_fact_binding_required",
                        "dfm_project_id": active_project_id,
                        "dfm_fact_name": open_facts[0]["clarification_id"].removeprefix("clarification_"),
                    }, ensure_ascii=False)
    if dfm_project_id or dfm_fact_name:
        if not dfm_project_id or not dfm_fact_name:
            return json.dumps({"error": "Both dfm_project_id and dfm_fact_name are required."})
        from tools.dfm.errors import DFMError
        from tools.dfm.service import get_dfm_service

        try:
            dfm_service = get_dfm_service()
            project = dfm_service.project("status", project_id=dfm_project_id)["project"]
            def canonical_ref(value):
                name, _, check_id = value.partition("@")
                return dfm_service._canonical_fact_name(name), check_id.casefold()

            requested_ref = canonical_ref(dfm_fact_name)
            pending = next((
                item for item in project["open_clarifications"]
                if canonical_ref(
                    str(item.get("clarification_id") or "")
                    .removeprefix("clarification_")
                ) == requested_ref
            ), None)
            if pending is None:
                return json.dumps({"error": "This DFM fact is not awaiting a user answer."})
            fact_name = pending["clarification_id"].removeprefix("clarification_")
            question = pending["question"]
            choices = list(dfm_service.clarification_choices(
                project.get("process") or dfm_service.config.default_process,
                fact_name,
            ))
        except DFMError as exc:
            return json.dumps(exc.to_dict(), ensure_ascii=False)
    # Generic clarifications retain their existing four-choice limit. DFM
    # choices come from the published ontology and must not be truncated.
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # LLMs sometimes emit dict-shaped choices (e.g. [{"description": "..."}])
        # instead of bare strings. _flatten_choice unwraps them to their
        # user-facing text here — the single platform-agnostic entry point —
        # so the CLI panel, Discord buttons, and Telegram list all render clean
        # text and the resolved answer is never a raw Python dict repr.
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if dfm_service is None and len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    if dfm_service is not None:
        if not str(user_response).strip():
            return json.dumps({"error": "The user did not answer the DFM fact."})
        try:
            confirmed = dfm_service.project(
                "confirm_fact",
                project_id=dfm_project_id,
                fact_name=fact_name,
                fact_value=str(user_response).strip(),
            )
        except DFMError as exc:
            return json.dumps(exc.to_dict(), ensure_ascii=False)
    else:
        confirmed = None

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
        **({"dfm_fact": confirmed["fact"]} if confirmed is not None else {}),
        **(
            {"next_action": confirmed["next_action"]}
            if confirmed is not None and "next_action" in confirmed else {}
        ),
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes. For a DFM "
        "open clarification, provide dfm_project_id and dfm_fact_name and omit "
        "choices. An unbound question during an active DFM clarification is "
        "rejected without showing it to the user. The tool fetches every "
        "published option and records the user's answer without a separate "
        "confirmation call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
            "dfm_project_id": {
                "type": "string",
                "description": "DFM project ID when answering a DFM open clarification.",
            },
            "dfm_fact_name": {
                "type": "string",
                "description": "DFM factor reference from the open clarification (name or name@check_id).",
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        callback=kw.get("callback"),
        dfm_project_id=args.get("dfm_project_id"),
        dfm_fact_name=args.get("dfm_fact_name"),
        session_id=kw.get("session_id")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
