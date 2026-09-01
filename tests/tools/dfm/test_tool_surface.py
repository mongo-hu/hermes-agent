import json
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


STEP_FIXTURE = Path("tests/fixtures/dfm/step/injection_plate_with_hole.step").resolve()


def test_dfm_tools_are_discovered_with_stable_schemas_and_dispatch(tmp_path):
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    assert registry.get_tool_names_for_toolset("dfm") == ["dfm_analysis", "dfm_project"]
    project_schema = registry.get_schema("dfm_project")
    analysis_schema = registry.get_schema("dfm_analysis")
    assert project_schema["parameters"]["properties"]["action"]["enum"] == [
        "create",
        "add_input",
        "status",
        "confirm_fact",
        "list",
    ]
    assert analysis_schema["parameters"]["properties"]["action"]["enum"] == [
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
    ]
    assert "observations" in analysis_schema["parameters"]["properties"]
    assert "fusion_links" in analysis_schema["parameters"]["properties"]

    token = set_hermes_home_override(tmp_path / "home")
    try:
        result = json.loads(
            registry.dispatch("dfm_project", {"action": "create", "name": "Bracket"})
        )
    finally:
        reset_hermes_home_override(token)
    assert result["ok"] is True


def test_dfm_project_resolves_quoted_desktop_ref_from_task_cwd(tmp_path):
    from tools.dfm.service import get_dfm_service
    from tools.registry import discover_builtin_tools, registry
    from tools.terminal_tool import (
        clear_task_env_overrides,
        register_task_env_overrides,
    )

    workspace = tmp_path / "session workspace"
    attachment = workspace / ".hermes" / "desktop-attachments" / "mold bracket.step"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(STEP_FIXTURE.read_bytes())
    task_id = "desktop-session"
    token = set_hermes_home_override(tmp_path / "home")
    register_task_env_overrides(task_id, {"cwd": str(workspace)})
    discover_builtin_tools()

    try:
        created = json.loads(
            registry.dispatch(
                "dfm_project",
                {"action": "create", "name": "Desktop upload"},
                task_id=task_id,
            )
        )
        added = json.loads(
            registry.dispatch(
                "dfm_project",
                {
                    "action": "add_input",
                    "project_id": created["project_id"],
                    "path": "@file:`.hermes/desktop-attachments/mold bracket.step`",
                },
                task_id=task_id,
            )
        )
    finally:
        get_dfm_service().close()
        clear_task_env_overrides(task_id)
        reset_hermes_home_override(token)

    assert added["ok"] is True
    assert added["input"]["source_name"] == "mold bracket.step"


def test_dfm_start_receives_internal_progress_context_without_schema_changes(
    monkeypatch,
):
    from tools import dfm_tool
    from tools.registry import discover_builtin_tools, registry

    captured = {}

    class FakeService:
        def analysis(self, action, **params):
            captured.update(params)
            return {"ok": True, "action": action}

    callback = lambda *_args, **_kwargs: None
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: FakeService())
    discover_builtin_tools()

    result = json.loads(
        registry.dispatch(
            "dfm_analysis",
            {"action": "start", "project_id": "dfm_1", "plan_id": "plan_1"},
            tool_progress_callback=callback,
            tool_call_id="tool_1",
        )
    )

    assert result["ok"] is True
    assert captured["_tool_progress_callback"] is callback
    assert captured["_tool_call_id"] == "tool_1"


def test_dfm_agent_tool_rejects_cancel_even_if_called_outside_its_schema(monkeypatch):
    from tools import dfm_tool

    class FakeService:
        def analysis(self, action, **params):
            raise AssertionError("Agent cancellation must not reach the service")

    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: FakeService())

    result = json.loads(
        dfm_tool._call(
            "analysis",
            {"action": "cancel", "project_id": "dfm_1", "run_id": "run_1"},
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "user_action_required"


def test_dfm_toolset_is_default_off_but_explicitly_configurable():
    from hermes_cli.tools_config import (
        CONFIGURABLE_TOOLSETS,
        _DEFAULT_OFF_TOOLSETS,
        _get_platform_tools,
    )
    from toolsets import resolve_toolset

    assert "dfm" in {item[0] for item in CONFIGURABLE_TOOLSETS}
    assert "dfm" in _DEFAULT_OFF_TOOLSETS
    assert "dfm" not in _get_platform_tools(
        {}, "cli", include_default_mcp_servers=False
    )
    enabled = _get_platform_tools(
        {"platform_toolsets": {"cli": ["dfm"]}},
        "cli",
        include_default_mcp_servers=False,
    )
    assert "dfm" in enabled
    assert set(resolve_toolset("dfm")) == {"dfm_project", "dfm_analysis"}


def test_dfm_is_not_part_of_core_platform_tools():
    from toolsets import _HERMES_CORE_TOOLS

    assert "dfm_project" not in _HERMES_CORE_TOOLS
    assert "dfm_analysis" not in _HERMES_CORE_TOOLS
