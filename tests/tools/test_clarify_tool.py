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


@pytest.fixture
def reviewed_dfm(monkeypatch):
    """A review with project-wide and feature-scoped analysis questions."""
    from types import SimpleNamespace

    class Service:
        config = SimpleNamespace(default_process="injection")
        review_pending = False
        blocked_action = None
        confirmations = 0
        pending = [
            {"clarification_id": "clarification_material", "question": "Material?"},
            {"clarification_id": "clarification_finish#wall", "question": "Wall finish?"},
        ]
        facts = []

        def project(self, action, **kwargs):
            assert kwargs["project_id"] == "dfm_test"
            if action == "status":
                return {"project": {
                    "process": "injection", "open_clarifications": self.pending,
                    "capabilities": {"discovery_review": {
                        "status": "pending" if self.review_pending else "confirmed",
                    }},
                }}
            assert action == "confirm_fact"
            self.facts.append({"name": kwargs["fact_name"], "value": kwargs["fact_value"]})
            self.pending.pop(0)
            return {"fact": self.facts[-1], "next_action": "clarify" if self.pending else "plan"}

        def analysis(self, action, **kwargs):
            assert action == "confirm_discovery"
            assert kwargs["project_id"] == "dfm_test"
            self.confirmations += 1
            self.review_pending = False
            return {"viewer_manifest": "viewer.json"}

        def review_state(self, project_id):
            assert project_id == "dfm_test"
            action = self.blocked_action or ("clarify" if self.pending else "plan")
            call = None
            if action == "clarify":
                call = {"tool": "clarify", "arguments": {
                    "dfm_project_id": project_id,
                    "dfm_fact_name": self.pending[0]["clarification_id"].removeprefix("clarification_"),
                }}
            elif action == "plan":
                call = {"tool": "dfm_analysis", "arguments": {"action": "plan", "project_id": project_id}}
            return {
                "snapshot_id": "snapshot-reviewed", "revision": 7,
                "next_action": action, "next_call": call, "continuation_required": call is not None,
                "instruction": "Follow next_call.", "open_clarification_count": len(self.pending),
                "pending_review_group_count": int(self.review_pending),
                "viewer_manifest": "viewer.json",
            }

        @staticmethod
        def _canonical_fact_name(name):
            return name

        @staticmethod
        def clarification_choices(_process, _name):
            return ["yes", "no"]

        @staticmethod
        def clarification_feature_refs(_project_id, name):
            return ["wall"] if "#wall" in name else []

    service = Service()
    monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: service)
    return service


class TestDiscoveryQuestionHandoff:
    @pytest.mark.parametrize("review_pending", [False, True])
    def test_review_opens_batch_without_another_model_call(self, reviewed_dfm, review_pending):
        reviewed_dfm.review_pending = review_pending
        shown = []

        def desktop(question, choices, context=None):
            shown.append(context)
            if context["mode"] == "dfm_discovery_review":
                assert context["snapshot_id"] == "snapshot-reviewed"
                assert context["revision"] == 7
                reviewed_dfm.review_pending = False  # The right-side save committed first.
                return choices[0]
            assert context["mode"] == "dfm_analysis_batch"
            assert context["project_id"] == "dfm_test"
            assert context["items"][1]["feature_refs"] == ["wall"]
            return json.dumps({"answers": [
                {"fact_name": item["fact_name"], "value": "yes"} for item in context["items"]
            ]})
        desktop.supports_dfm_review_context = lambda: True

        result = json.loads(clarify_tool(
            "", callback=desktop, dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert len(shown) == (2 if review_pending else 1)
        assert reviewed_dfm.confirmations == 0
        assert [fact["name"] for fact in reviewed_dfm.facts] == ["material", "finish#wall"]
        assert result["dfm_facts"] == reviewed_dfm.facts
        assert result["viewer_manifest"] == "viewer.json"
        assert result["dfm_discovery_review"]["open_clarification_count"] == 0
        assert result["next_call"]["arguments"]["action"] == "plan"
        assert result["next_action"] == "plan"

    @pytest.mark.parametrize("action", ["confirm_discovery", "wait_review_save", "retry_review_save"])
    def test_changes_during_input_use_latest_review_continuation(self, reviewed_dfm, action):
        def desktop(_question, _choices, context=None):
            reviewed_dfm.blocked_action = action
            return json.dumps({"answers": [
                {"fact_name": item["fact_name"], "value": "yes"} for item in context["items"]
            ]})

        result = json.loads(clarify_tool(
            "", callback=desktop, dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert "error" not in result
        assert len(result["dfm_facts"]) == 2
        for key in ("next_action", "next_call", "continuation_required", "instruction"):
            assert result[key] == result["dfm_discovery_review"][key]
        assert result["next_action"] == action
        assert reviewed_dfm.confirmations == 0

    @pytest.mark.parametrize("review_pending", [False, True])
    def test_two_argument_callbacks_keep_existing_continuation(self, reviewed_dfm, review_pending):
        reviewed_dfm.review_pending = review_pending
        shown = []
        result = json.loads(clarify_tool(
            "", callback=lambda question, choices: shown.append(question) or choices[0],
            dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert len(shown) == int(review_pending)
        assert result["next_call"]["arguments"]["dfm_fact_name"] == "material"
        assert not reviewed_dfm.facts

    @pytest.mark.parametrize("action", ["confirm_discovery", "wait_review_save", "retry_review_save", "plan"])
    def test_other_next_actions_are_not_automatically_executed(self, reviewed_dfm, action):
        reviewed_dfm.blocked_action = action

        def desktop(_question, _choices, _context=None):
            pytest.fail("No input should be opened for this review state")

        result = json.loads(clarify_tool(
            "", callback=desktop, dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert result["next_action"] == action
        assert reviewed_dfm.confirmations == 0
        assert not reviewed_dfm.facts

    def test_no_open_questions_returns_plan_without_opening_form(self, reviewed_dfm):
        reviewed_dfm.pending.clear()
        result = json.loads(clarify_tool(
            "", callback=lambda q, c, context=None: pytest.fail("No questions"),
            dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert result["next_action"] == "plan"
        assert not reviewed_dfm.facts

    @pytest.mark.parametrize("changed", ["pending", "revision"])
    def test_desktop_response_never_confirms_new_edits_after_gateway_acceptance(self, reviewed_dfm, changed):
        reviewed_dfm.review_pending = True

        def desktop(_question, choices, context):
            reviewed_dfm.review_pending = changed == "pending"
            context["_accepted_state"] = {"snapshot_id": "snapshot-reviewed", "revision": 6 if changed == "revision" else 7}
            return choices[0]
        desktop.supports_dfm_review_context = lambda: True

        result = json.loads(clarify_tool(
            "", callback=desktop, dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert "error" in result
        assert reviewed_dfm.confirmations == 0
        assert not reviewed_dfm.facts

    @pytest.mark.parametrize("answer", ["", "cancel", RuntimeError("input closed")])
    def test_incomplete_review_never_opens_parameters(self, reviewed_dfm, answer):
        reviewed_dfm.review_pending = True

        def desktop(_question, _choices, context=None):
            assert context["mode"] == "dfm_discovery_review"
            if isinstance(answer, Exception):
                raise answer
            return answer
        desktop.supports_dfm_review_context = lambda: True

        result = json.loads(clarify_tool(
            "", callback=desktop, dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert "error" in result
        assert reviewed_dfm.confirmations == 0
        assert not reviewed_dfm.facts

    @pytest.mark.parametrize("answer", ["", '{"answers": []}', RuntimeError("input closed")])
    def test_unanswered_batch_keeps_review_and_questions(self, reviewed_dfm, answer):
        def desktop(_question, _choices, context=None):
            assert context["mode"] == "dfm_analysis_batch"
            if isinstance(answer, Exception):
                raise answer
            return answer

        result = json.loads(clarify_tool(
            "", callback=desktop, dfm_project_id="dfm_test", dfm_discovery_review=True,
        ))
        assert "error" in result
        assert "dfm_facts" not in result
        assert result["dfm_discovery_review"]["open_clarification_count"] == 2
        assert reviewed_dfm.confirmations == 0
        assert not reviewed_dfm.facts


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
        contexts = []

        def desktop_callback(_question, options, context=None):
            contexts.append(context)
            return options[-1]

        result = json.loads(clarify_tool(
            "选择工艺", choices=["injection", "die_casting"],
            callback=desktop_callback,
            dfm_project_id="dfm_test", dfm_fact_name="process",
        ))
        assert result["choices_offered"] == ["压铸", "注塑成型"]
        assert result["user_response"] == "注塑成型"
        assert confirmed[0]["fact_value"] == "注塑成型"
        assert contexts == [None]

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

    def test_desktop_batches_dynamic_analysis_questions_once(self, monkeypatch):
        from types import SimpleNamespace

        pending = [
            {"clarification_id": "clarification_material", "question": "Material?"},
            {
                "clarification_id": "clarification_fill@check.boss",
                "question": "Boss fill?",
            },
        ]
        confirmed = []

        class FakeService:
            config = SimpleNamespace(default_process="injection")

            @staticmethod
            def review_state(_project_id):
                return {"snapshot_id": "snapshot-current", "revision": 4}

            @staticmethod
            def _canonical_fact_name(name):
                return name

            @staticmethod
            def clarification_choices(_process, name):
                return ["ABS", "PC"] if name == "material" else ["unfilled", "GF"]

            @staticmethod
            def clarification_feature_refs(_project_id, name):
                return ("feature.boss.1",) if "check.boss" in name else ()

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
                confirmed.append(kwargs)
                pending.pop(0)
                return {
                    "fact": {
                        "name": kwargs["fact_name"],
                        "value": kwargs["fact_value"],
                    },
                    "next_action": "clarify" if pending else "plan",
                }

        monkeypatch.setattr("tools.dfm.service.get_dfm_service", FakeService)
        shown = []

        def desktop_callback(question, choices, context):
            shown.append((question, choices, context))
            return json.dumps({
                "answers": [
                    {"fact_name": "material", "value": "ABS"},
                    {"fact_name": "fill@check.boss", "value": "GF"},
                ]
            })

        result = json.loads(clarify_tool(
            "",
            callback=desktop_callback,
            dfm_project_id="dfm_test",
            dfm_fact_name="material",
        ))

        assert len(shown) == 1
        assert shown[0][1] is None
        assert shown[0][2]["mode"] == "dfm_analysis_batch"
        assert shown[0][2]["items"][1]["feature_refs"] == ["feature.boss.1"]
        assert [item["fact_name"] for item in confirmed] == [
            "material",
            "fill@check.boss",
        ]
        assert result["next_action"] == "plan"
        assert len(result["dfm_facts"]) == 2
        assert result["next_call"] == {
            "tool": "dfm_analysis",
            "arguments": {"action": "plan", "project_id": "dfm_test"},
        }

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

            @staticmethod
            def review_state(project_id):
                return {
                    "status": "discovery_confirmed", "next_action": "plan",
                    "next_call": {"tool": "dfm_analysis", "arguments": {"action": "plan", "project_id": project_id}},
                    "continuation_required": True, "instruction": "Continue with next_call.",
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
