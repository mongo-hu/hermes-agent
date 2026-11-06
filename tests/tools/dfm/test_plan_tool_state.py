"""Tool summaries preserve executable and human-input continuations."""
import pytest

from tools.dfm_tool import _plan_tool_summary


@pytest.mark.parametrize("status,pending,next_action", [
    ("waiting_input", [{"clarification_id": "clarification_material#feature-wall"}], "clarify"),
    ("waiting_input", [], "review_forming_scopes"),
    ("ready", [], "start"),
])
def test_fragment_plan_control_survives_compaction(status, pending, next_action):
    plan = {"plan_id": "plan-current", "status": status, "phase": "analysis",
            "rule_bindings": [], "plan_fragments": [{"fragment_id": "fragment-current"}],
            "pending_inputs": pending}
    result = _plan_tool_summary({"ok": True, "project_id": "project-current", "plan": plan,
                                "capability": {"status": "available"}})
    assert result["plan"]["plan_fragment_count"] == 1
    assert result["next_action"] == next_action
    if pending:
        assert result["next_call"] == {"tool": "clarify", "arguments": {
            "dfm_project_id": "project-current", "dfm_fact_name": "material#feature-wall"}}
        assert result["requires_user_response"]
        assert result["open_clarification_count"] == len(pending)
    elif status == "waiting_input":
        assert result["next_call"] is None
        assert result["requires_user_response"]
        assert not result["continuation_required"]
    else:
        assert result["next_call"]["arguments"]["plan_id"] == plan["plan_id"]
        assert result["continuation_required"]
