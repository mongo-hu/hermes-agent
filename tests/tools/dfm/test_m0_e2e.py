"""M0 vertical-slice acceptance tests for the built-in DFM capability."""

from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from model_tools import get_tool_definitions
from tools.dfm.analyzers.base import AnalyzerContext
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.config import DFMConfig
from tools.dfm.contracts import (
    Capability,
    CapabilityStatus,
)
from tools.dfm.errors import DFMError
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.service import DFMService, get_dfm_service
from tools.registry import discover_builtin_tools, registry


STEP_FIXTURE = Path("tests/fixtures/dfm/step/injection_plate_with_hole.step").resolve()


def _confirm_step_facts(project_id: str) -> None:
    for name, value in {
        "process": "injection",
        "material": "ABS",
        "pull_dir": [0, 0, 1],
        "model_units": "mm",
    }.items():
        _dispatch(
            "dfm_project",
            {
                "action": "confirm_fact",
                "project_id": project_id,
                "fact_name": name,
                "fact_value": value,
            },
        )

def _tool_names(toolsets: list[str]) -> set[str]:
    return {
        definition["function"]["name"]
        for definition in get_tool_definitions(enabled_toolsets=toolsets)
    }


def _dispatch(name: str, arguments: dict) -> dict:
    return json.loads(registry.dispatch(name, arguments))


def test_production_step_intake_rejects_fake_content_without_fake_results(tmp_path):
    discover_builtin_tools()
    core_names = _tool_names(["hermes-cli"])
    enabled_definitions = get_tool_definitions(
        enabled_toolsets=["hermes-cli", "dfm"]
    )
    enabled_names = {
        definition["function"]["name"] for definition in enabled_definitions
    }

    assert {"dfm_project", "dfm_analysis"}.isdisjoint(core_names)
    assert {"dfm_project", "dfm_analysis"} <= enabled_names

    token = set_hermes_home_override(tmp_path / "home")
    source = tmp_path / "opaque.step"
    source.write_bytes(b"synthetic opaque STEP payload")
    try:
        created = _dispatch(
            "dfm_project",
            {"action": "create", "name": "M0 production acceptance"},
        )
        project_id = created["project_id"]
        added = _dispatch(
            "dfm_project",
            {
                "action": "add_input",
                "project_id": project_id,
                "path": f"@file:{source}",
            },
        )
        status = _dispatch(
            "dfm_project",
            {"action": "status", "project_id": project_id},
        )
        final_status = _dispatch(
            "dfm_project",
            {"action": "status", "project_id": project_id},
        )
        schemas_after = get_tool_definitions(
            enabled_toolsets=["hermes-cli", "dfm"]
        )
    finally:
        get_dfm_service().close()
        reset_hermes_home_override(token)

    assert added["ok"] is False
    assert added["error"]["code"] == "step_format_invalid"
    assert status["project"]["input_mode"] is None
    assert final_status["project"]["run_count"] == 0
    assert final_status["project"]["finding_count"] == 0
    assert final_status["project"]["artifact_count"] == 0
    assert schemas_after == enabled_definitions


class SuccessfulTestAnalyzer:
    key = "step"
    version = "m0-test-only"
    supported_inputs = ("step",)

    def capability(self, context: AnalyzerContext) -> Capability:
        return Capability(self.key, CapabilityStatus.AVAILABLE, "test only")

    def run(self, context: AnalyzerContext, cancellation):
        raise AssertionError("the removed internal route must not execute")


def test_injected_internal_analyzer_cannot_bypass_the_v2_contract(tmp_path):
    token = set_hermes_home_override(tmp_path / "home")
    analyzer_registry = AnalyzerRegistry()
    analyzer_registry.register(SuccessfulTestAnalyzer())
    service = DFMService(
        config=DFMConfig(
            max_concurrent_runs=1, geometry_backend="pythonocc_internal"
        ),
        workspace=DFMWorkspace(),
        registry=analyzer_registry,
        reconcile_jobs=False,
    )
    source = tmp_path / "accepted.step"
    source.write_bytes(STEP_FIXTURE.read_bytes())
    try:
        project_id = service.project("create", name="M0 success acceptance")[
            "project_id"
        ]
        service.project(
            "add_input",
            project_id=project_id,
            path=f"@file:{source}",
        )
        for name, value in {
            "process": "injection",
            "material": "ABS",
            "pull_dir": [0, 0, 1],
            "model_units": "mm",
        }.items():
            service.project(
                "confirm_fact",
                project_id=project_id,
                fact_name=name,
                fact_value=value,
            )
        discovery = service.analysis("discover", project_id=project_id)
        if discovery.get("status") == "discovery_review_required":
            service.analysis(
                "confirm_discovery",
                project_id=project_id,
                expected_revision=discovery["revision"],
            )
        with pytest.raises(DFMError) as exc_info:
            service.analysis("plan", project_id=project_id)
    finally:
        service.close()
        reset_hermes_home_override(token)

    assert exc_info.value.code == "geometry_contract_unsupported"


def test_desktop_file_attach_reference_flows_into_dfm_project(monkeypatch, tmp_path):
    from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides
    from tui_gateway import server

    workspace = tmp_path / "desktop workspace"
    workspace.mkdir()
    task_id = "dfm-desktop-session"
    fake_cli = types.ModuleType("cli")
    fake_cli._detect_file_drop = lambda raw: None
    fake_cli._split_path_input = lambda raw: (raw, "")
    fake_cli._resolve_attachment_path = lambda raw: None
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    server._sessions[task_id] = {
        "session_key": task_id,
        "cwd": str(workspace),
        "attached_images": [],
    }
    token = set_hermes_home_override(tmp_path / "home")
    register_task_env_overrides(task_id, {"cwd": str(workspace)})
    discover_builtin_tools()

    try:
        attached = server.handle_request(
            {
                "id": "attach-1",
                "method": "file.attach",
                "params": {
                    "session_id": task_id,
                    "name": "mold bracket.step",
                    "data_url": "data:application/octet-stream;base64,"
                    + base64.b64encode(STEP_FIXTURE.read_bytes()).decode("ascii"),
                },
            }
        )["result"]
        created = json.loads(
            registry.dispatch(
                "dfm_project",
                {"action": "create", "name": "Desktop E2E"},
                task_id=task_id,
            )
        )
        added = json.loads(
            registry.dispatch(
                "dfm_project",
                {
                    "action": "add_input",
                    "project_id": created["project_id"],
                    "path": attached["ref_text"],
                },
                task_id=task_id,
            )
        )
    finally:
        get_dfm_service().close()
        clear_task_env_overrides(task_id)
        reset_hermes_home_override(token)
        server._sessions.pop(task_id, None)

    assert attached["ref_text"] == "@file:`.hermes/desktop-attachments/mold bracket.step`"
    assert added["ok"] is True
    assert added["input"]["source_name"] == "mold bracket.step"
