"""Tests for tools/clarify_tool.py - Interactive clarifying questions."""

import json
from typing import List, Optional

import pytest

from tools.clarify_tool import (
    clarify_tool,
    check_clarify_requirements,
    MAX_CHOICES,
    CLARIFY_SCHEMA,
    _flatten_choice,
)


class TestClarifyToolBasics:
    """Basic functionality tests for clarify_tool."""

    def test_simple_question_with_callback(self):
        """Should return user response for simple question."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "What color?"
            assert choices is None
            return "blue"

        result = json.loads(clarify_tool("What color?", callback=mock_callback))
        assert result["question"] == "What color?"
        assert result["choices_offered"] is None
        assert result["user_response"] == "blue"

    def test_question_with_choices(self):
        """Should pass choices to callback and return response."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "Pick a number"
            assert choices == ["1", "2", "3"]
            return "2"

        result = json.loads(clarify_tool(
            "Pick a number",
            choices=["1", "2", "3"],
            callback=mock_callback
        ))
        assert result["question"] == "Pick a number"
        assert result["choices_offered"] == ["1", "2", "3"]
        assert result["user_response"] == "2"

    def test_empty_question_returns_error(self):
        """Should return error for empty question."""
        result = json.loads(clarify_tool("", callback=lambda q, c: "ignored"))
        assert "error" in result
        assert "required" in result["error"].lower()

    def test_whitespace_only_question_returns_error(self):
        """Should return error for whitespace-only question."""
        result = json.loads(clarify_tool("   \n\t  ", callback=lambda q, c: "ignored"))
        assert "error" in result

    def test_no_callback_returns_error(self):
        """Should return error when no callback is provided."""
        result = json.loads(clarify_tool("What do you want?"))
        assert "error" in result
        assert "not available" in result["error"].lower()


class TestClarifyToolChoicesValidation:
    """Tests for choices parameter validation."""

    def test_choices_trimmed_to_max(self):
        """Should trim choices to MAX_CHOICES."""
        choices_passed = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_passed.extend(choices or [])
            return "picked"

        many_choices = ["a", "b", "c", "d", "e", "f", "g"]
        clarify_tool("Pick one", choices=many_choices, callback=mock_callback)

        assert len(choices_passed) == MAX_CHOICES

    def test_dfm_choices_are_complete_and_come_from_ontology(self, monkeypatch):
        from types import SimpleNamespace

        published = [f"option-{index}" for index in range(12)]
        confirmed = []

        class FakeService:
            config = SimpleNamespace(default_process="injection")

            @staticmethod
            def clarification_choices(_process, _name):
                return published

            @staticmethod
            def _canonical_fact_name(name):
                return name

            @staticmethod
            def project(action, **kwargs):
                if action == "status":
                    return {"project": {"process": "injection", "open_clarifications": [
                        {"clarification_id": "clarification_mat_family", "question": "材料类别？"},
                    ]}}
                confirmed.append(kwargs)
                return {"fact": {"name": kwargs["fact_name"], "value": kwargs["fact_value"]}}

        monkeypatch.setattr("tools.dfm.service.get_dfm_service", FakeService)
        result = json.loads(clarify_tool(
            "材料类别？", choices=["wrong"],
            callback=lambda _question, options: options[-1],
            dfm_project_id="dfm_test",
            dfm_fact_name="clarification_mat_family",
        ))
        assert result["choices_offered"] == published
        assert result["user_response"] == published[-1]
        assert confirmed[0]["fact_value"] == published[-1]

    def test_dfm_process_choices_use_engineering_names(self, monkeypatch):
        from types import SimpleNamespace

        confirmed = []

        class FakeService:
            config = SimpleNamespace(default_process="injection")

            @staticmethod
            def _canonical_fact_name(name):
                return name

            @staticmethod
            def clarification_choices(_process, _name):
                return ["压铸", "注塑成型"]

            @staticmethod
            def project(action, **kwargs):
                if action == "status":
                    return {"project": {"open_clarifications": [
                        {"clarification_id": "clarification_process", "question": "请选择 DFM 分析工艺。"},
                    ]}}
                confirmed.append(kwargs)
                return {"fact": {"name": "process", "value": "injection"}}

        monkeypatch.setattr("tools.dfm.service.get_dfm_service", FakeService)
        result = json.loads(clarify_tool(
            "选择工艺", choices=["injection", "die_casting"],
            callback=lambda _question, options: options[-1],
            dfm_project_id="dfm_test", dfm_fact_name="process",
        ))
        assert result["choices_offered"] == ["压铸", "注塑成型"]
        assert result["user_response"] == "注塑成型"
        assert confirmed[0]["fact_value"] == "注塑成型"

    def test_unbound_dfm_question_binds_next_open_factor_before_showing_ui(
        self, monkeypatch
    ):
        from tools import dfm_tool
        from types import SimpleNamespace

        monkeypatch.setattr(dfm_tool, "_session_projects", {"session-one": "dfm_test"})
        pending = [{
            "clarification_id": "clarification_mat_additive_fill",
            "question": "Material fill?",
        }]
        confirmed = []

        class FakeService:
            config = SimpleNamespace(default_process="injection")

            @staticmethod
            def _canonical_fact_name(name):
                return name

            @staticmethod
            def clarification_choices(_process, _name):
                return ["Unfilled", "GF"]

            @staticmethod
            def project(action, **kwargs):
                assert kwargs["project_id"] == "dfm_test"
                if action == "status":
                    return {
                        "project": {
                            "process": "injection",
                            "open_clarifications": pending,
                        }
                    }
                assert action == "confirm_fact"
                confirmed.append(kwargs)
                pending.clear()
                return {
                    "fact": {
                        "name": kwargs["fact_name"],
                        "value": kwargs["fact_value"],
                    },
                    "next_action": "discover",
                }

        monkeypatch.setattr("tools.dfm.service.get_dfm_service", FakeService)
        shown = []
        result = json.loads(clarify_tool(
            "Material fill?", choices=["Unfilled", "GF"],
            callback=lambda question, choices: shown.append((question, choices)) or "non-LGF",
            session_id="session-one",
        ))
        assert shown == [("Material fill?", ["Unfilled", "GF"])]
        assert result["dfm_fact"] == {
            "name": "mat_additive_fill",
            "value": "non-LGF",
        }
        assert result["continuation_required"] is True
        assert result["next_call"] == {
            "tool": "dfm_analysis",
            "arguments": {"action": "discover", "project_id": "dfm_test"},
        }
        assert confirmed[0]["fact_name"] == "mat_additive_fill"
        assert confirmed[0]["fact_value"] == "non-LGF"

        shown.clear()
        ordinary = json.loads(clarify_tool(
            "Another question", choices=["yes", "no"],
            callback=lambda question, choices: shown.append((question, choices)) or "yes",
            session_id="session-one",
        ))
        assert shown == [("Another question", ["yes", "no"])]
        assert ordinary["user_response"] == "yes"
        assert "dfm_fact" not in ordinary

    def test_dfm_answer_returns_exact_next_clarification_call(self, monkeypatch):
        from types import SimpleNamespace

        pending = [
            {"clarification_id": "clarification_first", "question": "First?"},
            {"clarification_id": "clarification_second", "question": "Second?"},
        ]

        class FakeService:
            config = SimpleNamespace(default_process="injection")

            @staticmethod
            def _canonical_fact_name(name):
                return name

            @staticmethod
            def clarification_choices(_process, _name):
                return ["yes", "no"]

            @staticmethod
            def project(action, **kwargs):
                if action == "status":
                    return {
                        "project": {
                            "process": "injection",
                            "open_clarifications": pending,
                        }
                    }
                assert action == "confirm_fact"
                pending.pop(0)
                return {
                    "fact": {"name": "first", "value": kwargs["fact_value"]},
                    "next_action": "clarify",
                }

        monkeypatch.setattr("tools.dfm.service.get_dfm_service", FakeService)

        result = json.loads(
            clarify_tool(
                "",
                callback=lambda _question, _choices: "yes",
                dfm_project_id="dfm_test",
                dfm_fact_name="first",
            )
        )

        assert result["continuation_required"] is True
        assert result["next_call"] == {
            "tool": "clarify",
            "arguments": {
                "dfm_project_id": "dfm_test",
                "dfm_fact_name": "second",
            },
        }
        assert "Do not end the turn" in result["instruction"]

    @pytest.mark.parametrize("requested_review", [False, True])
    def test_unbound_question_binds_pending_discovery_review_once(
        self, monkeypatch, requested_review
    ):
        from tools import dfm_tool

        monkeypatch.setattr(dfm_tool, "_session_projects", {"session-one": "dfm_test"})
        confirmed = []

        class FakeService:
            @staticmethod
            def project(action, **kwargs):
                assert action == "status"
                assert kwargs["project_id"] == "dfm_test"
                return {"project": {
                    "capabilities": {"discovery_review": {"status": "pending"}},
                    "open_clarifications": [],
                }}

            @staticmethod
            def analysis(action, **kwargs):
                assert action == "confirm_discovery"
                confirmed.append(kwargs["project_id"])
                return {
                    "status": "discovery_confirmed",
                    "next_action": "plan",
                    "viewer_manifest": "dfm_viewer.json",
                }

        monkeypatch.setattr("tools.dfm.service.get_dfm_service", FakeService)
        shown = []
        result = json.loads(clarify_tool(
            "" if requested_review else "Agent-authored duplicate prompt",
            callback=lambda question, choices: shown.append((question, choices))
            or choices[0],
            dfm_discovery_review=requested_review,
            session_id="session-one",
        ))

        assert len(shown) == 1
        assert confirmed == ["dfm_test"]
        assert result["dfm_discovery_review"]["status"] == "discovery_confirmed"
        assert result["next_action"] == "plan"

    def test_dfm_project_call_binds_its_session(self, monkeypatch):
        from tools import dfm_tool

        monkeypatch.setattr(dfm_tool, "_session_projects", {})

        class FakeService:
            @staticmethod
            def project(action, **kwargs):
                assert action == "create"
                return {"ok": True, "project_id": "dfm_test"}

        monkeypatch.setattr(dfm_tool, "get_dfm_service", FakeService)
        result = json.loads(dfm_tool._call(
            "project", {"action": "create", "name": "test"}, session_id="session-one",
        ))
        assert result["project_id"] == "dfm_test"
        assert dfm_tool.active_dfm_project_id("session-one") == "dfm_test"

    def test_empty_choices_become_none(self):
        """Empty choices list should become None (open-ended)."""
        choices_received = ["marker"]

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.clear()
            if choices is not None:
                choices_received.extend(choices)
            return "answer"

        clarify_tool("Open question?", choices=[], callback=mock_callback)
        assert choices_received == []  # Was cleared, nothing added

    def test_choices_with_only_whitespace_stripped(self):
        """Whitespace-only choices should be stripped out."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=["valid", "  ", "", "also valid"], callback=mock_callback)
        assert choices_received == ["valid", "also valid"]

    def test_invalid_choices_type_returns_error(self):
        """Non-list choices should return error."""
        result = json.loads(clarify_tool(
            "Question?",
            choices="not a list",  # type: ignore
            callback=lambda q, c: "ignored"
        ))
        assert "error" in result
        assert "list" in result["error"].lower()

    def test_choices_converted_to_strings(self):
        """Non-string choices should be converted to strings."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=[1, 2, 3], callback=mock_callback)  # type: ignore
        assert choices_received == ["1", "2", "3"]


class TestClarifyToolCallbackHandling:
    """Tests for callback error handling."""

    def test_callback_exception_returns_error(self):
        """Should return error if callback raises exception."""
        def failing_callback(question: str, choices: Optional[List[str]]) -> str:
            raise RuntimeError("User cancelled")

        result = json.loads(clarify_tool("Question?", callback=failing_callback))
        assert "error" in result
        assert "Failed to get user input" in result["error"]
        assert "User cancelled" in result["error"]

    def test_callback_receives_stripped_question(self):
        """Callback should receive trimmed question."""
        received_question = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            received_question.append(question)
            return "answer"

        clarify_tool("  Question with spaces  \n", callback=mock_callback)
        assert received_question[0] == "Question with spaces"

    def test_user_response_stripped(self):
        """User response should be stripped of whitespace."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            return "  response with spaces  \n"

        result = json.loads(clarify_tool("Q?", callback=mock_callback))
        assert result["user_response"] == "response with spaces"


class TestCheckClarifyRequirements:
    """Tests for the requirements check function."""

    def test_always_returns_true(self):
        """clarify tool has no external requirements."""
        assert check_clarify_requirements() is True


class TestClarifyDictChoices:
    """Dict-shaped choices must be unwrapped to user-facing text at the source.

    LLMs sometimes emit [{"description": "..."}] instead of bare strings. The
    naive str(c) coercion leaked the Python dict repr onto every surface (CLI
    panel, Discord buttons, Telegram list) AND returned it verbatim as the
    user's answer. _flatten_choice normalises at the one platform-agnostic
    entry point so the whole class is fixed in one place.
    """

    def test_flatten_unwraps_label_first(self):
        assert _flatten_choice({"label": "Short", "description": "Long"}) == "Short"

    def test_flatten_unwraps_description_when_no_label(self):
        assert _flatten_choice({"description": "A loose layout"}) == "A loose layout"

    def test_flatten_unwrap_order_label_over_description(self):
        assert _flatten_choice({"description": "verbose", "label": "tight"}) == "tight"

    def test_flatten_drops_name_value_only_dict(self):
        # name/value are component-shaped fields, not user-facing labels —
        # picking them would leak raw enum values / short model ids.
        assert _flatten_choice({"name": "tight", "value": "x"}) == ""

    def test_flatten_prefers_canonical_key_over_name(self):
        assert _flatten_choice({"name": "tight", "description": "Tight desc"}) == "Tight desc"

    def test_flatten_drops_keyless_dict(self):
        assert _flatten_choice({"foo": "bar", "n": 1}) == ""

    def test_flatten_passthrough_string_and_scalar(self):
        assert _flatten_choice("plain") == "plain"
        assert _flatten_choice(7) == "7"
        assert _flatten_choice(None) == ""

    def test_dict_choices_reach_callback_as_clean_text(self):
        """The whole point: the UI callback never sees a dict repr."""
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[0]

        result = json.loads(clarify_tool(
            "Pick a layout",
            choices=[
                {"choice": "Tight", "description": "Tight, covers all 3 points"},
                {"description": "Loose layout"},
                {"name": "modelid", "value": "abc"},  # dropped, not leaked
                "A plain string choice",
            ],
            callback=cb,
        ))  # type: ignore
        assert seen == [
            "Tight, covers all 3 points",
            "Loose layout",
            "A plain string choice",
        ]
        # and the resolved answer is clean text, not a dict repr
        assert result["user_response"] == "Tight, covers all 3 points"
        assert "{" not in result["user_response"]
        assert all("{" not in c for c in result["choices_offered"])


class TestClarifySchema:
    """Tests for the OpenAI function-calling schema."""

    def test_schema_name(self):
        """Schema should have correct name."""
        assert CLARIFY_SCHEMA["name"] == "clarify"

    def test_schema_has_description(self):
        """Schema should have a description."""
        assert "description" in CLARIFY_SCHEMA
        assert len(CLARIFY_SCHEMA["description"]) > 50

    def test_schema_question_optional_for_bound_dfm(self):
        """Bound DFM prompts may omit their tool-owned question text."""
        assert "question" not in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_choices_optional(self):
        """Choices parameter should be optional."""
        assert "choices" not in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_choices_max_items(self):
        """Schema should specify max items for choices."""
        choices_spec = CLARIFY_SCHEMA["parameters"]["properties"]["choices"]
        assert choices_spec.get("maxItems") == MAX_CHOICES

    def test_max_choices_is_four(self):
        """MAX_CHOICES constant should be 4."""
        assert MAX_CHOICES == 4
