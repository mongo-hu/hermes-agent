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
        "list",
        "ontology_status",
        "sync_ontology",
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
        "report_context",
        "result",
        "render_html",
        "context",
    ]
    assert "observations" in analysis_schema["parameters"]["properties"]
    assert "fusion_links" in analysis_schema["parameters"]["properties"]
    assert "llm_content" in analysis_schema["parameters"]["properties"]
    assert "process" not in project_schema["parameters"]["properties"]
    assert "process" not in analysis_schema["parameters"]["properties"]
    assert analysis_schema["parameters"]["properties"]["analyzer_key"]["enum"] == [
        "occt_cpp_external",
        "pythonocc_internal",
        "parasolid",
        "drawing",
        "fusion",
    ]
    wait_schema = analysis_schema["parameters"]["properties"]["wait_seconds"]
    assert wait_schema["minimum"] == 0
    assert wait_schema["maximum"] >= 30

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


def test_dfm_background_actions_receive_progress_context_without_schema_changes(
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

    for action, arguments in (
        ("start", {"plan_id": "plan_1"}),
        ("render_html", {"run_id": "run_1", "llm_content": {}}),
    ):
        captured.clear()
        result = json.loads(
            registry.dispatch(
                "dfm_analysis",
                {"action": action, "project_id": "dfm_1", **arguments},
                tool_progress_callback=callback,
                tool_call_id=f"tool_{action}",
            )
        )

        assert result["ok"] is True
        assert captured["_tool_progress_callback"] is callback
        assert captured["_tool_call_id"] == f"tool_{action}"


def test_discovery_tool_returns_bounded_counts_without_geometry_payload(monkeypatch):
    from tools import dfm_tool

    feature_kinds = ("ordinary_part", "main_wall", "screw_boss", "screw_boss")
    discovery = {
        "ok": True,
        "project_id": "dfm_1",
        "phase": "discovery",
        "plan": {"operations": ["large geometry" * 10_000]},
        "snapshot": {"snapshot_id": "snapshot_1", "status": "frozen"},
        "features": [
            {"feature_id": f"feature_{index}", "kind": kind, "status": "confirmed",
             "properties": {"raw": "geometry" * 10_000}}
            for index, kind in enumerate(feature_kinds)
        ],
        "regions": [
            {"region_id": f"region_{index}", "role": kind,
             "geometry_refs": ["face" * 10_000]}
            for index, kind in enumerate(feature_kinds)
        ],
        "observations": [],
        "fusion_links": [],
        "capability": {"status": "available", "providers": {"occt_cpp": "available"}},
        "drawing_discovery": {"status": "not_applicable"},
        "open_clarifications": [],
    }

    class FakeService:
        def analysis(self, action, **params):
            assert action == "discover"
            return discovery

    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: FakeService())
    output = dfm_tool._call("analysis", {"action": "discover", "project_id": "dfm_1"})
    summary = json.loads(output)

    assert len(output) < 10_000
    assert summary["feature_count"] == 4
    assert summary["feature_counts"] == {
        "ordinary_part": 1, "main_wall": 1, "screw_boss": 2,
    }
    assert summary["region_count"] == 4
    assert summary["snapshot"]["snapshot_id"] == "snapshot_1"
    assert summary["next_action"] == "plan"
    assert "geometry_refs" not in output
    assert "operations" not in output
    assert "properties" in discovery["features"][0]


def test_plan_and_start_tool_results_keep_control_data_not_full_snapshots(monkeypatch):
    from tools import dfm_tool

    class FakeService:
        def analysis(self, action, **params):
            if action == "plan":
                return {
                    "ok": True,
                    "project_id": "dfm_1",
                    "plan": {
                        "plan_id": "plan_1", "phase": "analysis", "status": "ready",
                        "scope_id": "rules", "scope_version": "v1",
                        "operations": [{"geometry": "face" * 30_000}],
                        "rule_bindings": [{"check_id": "C_WALL", "rule_id": "R_WALL"}],
                        "regions": [{"geometry_refs": ["face" * 30_000]}],
                    },
                    "capability": {"status": "available", "reason": "ready",
                                   "details": {"operations": ["face" * 30_000]}},
                }
            return {
                "ok": True,
                "project_id": "dfm_1",
                "run": {
                    "run_id": "run_1", "plan_id": "plan_1", "analyzer_key": "occt_cpp",
                    "status": "succeeded" if action == "result" else "queued",
                    "plan_snapshot": {"geometry": "face" * 30_000},
                    "artifacts": [{"artifact_id": "report_html", "kind": "report_html",
                                   "path": "C:/report.html"}] if action == "result" else [],
                },
            }

    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: FakeService())
    planned = dfm_tool._call("analysis", {"action": "plan", "project_id": "dfm_1"})
    started = dfm_tool._call(
        "analysis", {"action": "start", "project_id": "dfm_1", "plan_id": "plan_1"}
    )
    finished = dfm_tool._call(
        "analysis", {"action": "result", "project_id": "dfm_1", "run_id": "run_1"}
    )

    assert len(planned) < 10_000
    assert len(started) < 10_000
    assert json.loads(planned)["plan"]["check_ids"] == ["C_WALL"]
    assert json.loads(planned)["next_action"] == "start"
    assert json.loads(started)["run"]["run_id"] == "run_1"
    assert json.loads(started)["next_action"] == "report_context"
    assert json.loads(finished)["next_action"] == "complete"
    assert json.loads(finished)["run"]["artifacts"][0]["path"] == "C:/report.html"
    assert "geometry_refs" not in planned
    assert "plan_snapshot" not in started


def test_project_status_tool_exposes_state_without_persisted_geometry(monkeypatch):
    from tools import dfm_tool

    class FakeService:
        def project(self, action, **params):
            assert action == "status"
            return {
                "ok": True,
                "project": {
                    "project_id": "dfm_1", "name": "bracket", "process": "injection",
                    "input_mode": "step", "revision": 3,
                    "inputs": [{"input_id": "input_1", "kind": "step", "sha256": "abc"}],
                    "facts": [{"name": "model_units", "value": "mm", "status": "confirmed"}],
                    "open_clarifications": [],
                    "discovery_snapshots": [{"snapshot_id": "snapshot_1"}],
                    "plans": [{"regions": ["face" * 30_000]}],
                    "runs": [{"run_id": "run_1", "status": "running"}],
                    "findings": [{"finding_id": "finding_1"}],
                    "features": [{"properties": "face" * 30_000}],
                },
                "ontology": {"snapshot_id": "ontology_1"},
                "capabilities": {"occt_cpp": {"status": "available",
                                               "details": {"raw": "face" * 30_000}}},
                "process_capabilities": {},
            }

    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: FakeService())
    output = dfm_tool._call("project", {"action": "status", "project_id": "dfm_1"})
    summary = json.loads(output)

    assert len(output) < 10_000
    assert summary["project"]["discovery_snapshot_id"] == "snapshot_1"
    assert summary["project"]["finding_count"] == 1
    assert summary["capabilities"]["occt_cpp"] == {"status": "available"}
    assert "geometry" not in output


def test_dfm_start_schema_requires_explicit_planned_id():
    from tools.dfm_tool import DFM_ANALYSIS_SCHEMA

    description = DFM_ANALYSIS_SCHEMA["parameters"]["properties"]["plan_id"]["description"]
    assert "Required when action=start" in description
    assert "never infer" in description


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
