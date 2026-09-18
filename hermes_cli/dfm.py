"""Operator-facing diagnostics for the built-in DFM capability."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from tools.dfm.analyzers.base import AnalyzerContext
from tools.dfm.analyzers.registry import build_default_registry
from tools.dfm.analyzers.occt import ENGINE_VERSION
from tools.dfm.analyzers.step import dependency_statuses
from tools.dfm.config import load_dfm_config
from tools.dfm.contracts import (
    DISCOVERY_SCHEMA_VERSION,
    OBJECTIVE_SCHEMA_VERSION,
)
from tools.dfm.errors import DFMError
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.processes.registry import build_default_process_registry
from tools.dfm.workers import step_worker


def _html_report_diagnostics() -> dict:
    """Check the Agent Python used by the HTML editor's layout subprocess."""

    python_executable = sys.executable
    result = {
        "python_executable": python_executable,
        "playwright_installed": importlib.util.find_spec("playwright") is not None,
        "chromium_usable": False,
        "ready": False,
        "reason": "",
    }
    if not result["playwright_installed"]:
        result["reason"] = "Playwright is not installed in the Agent Python environment."
        return result

    # The renderer launches Playwright in a child process with sys.executable.
    # Probe that same boundary so this also works when doctor is invoked from
    # an asyncio-hosted Agent, where Playwright's sync API cannot run inline.
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as playwright:\n"
        "    browser = playwright.chromium.launch(headless=True)\n"
        "    browser.close()\n"
    )
    try:
        completed = subprocess.run(
            [python_executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            ),
        )
    except subprocess.TimeoutExpired:
        result["reason"] = "Playwright Chromium launch timed out."
        return result
    except OSError as exc:
        result["reason"] = f"Could not launch the Agent Python: {exc}"
        return result

    if completed.returncode != 0:
        result["reason"] = (
            "Playwright could not launch Chromium. Run "
            "python -m playwright install chromium in the Agent Python environment."
        )
        result["error"] = completed.stderr.strip()[-1500:]
        return result

    result["chromium_usable"] = True
    result["ready"] = True
    return result


def build_parser(subparsers):
    parser = subparsers.add_parser("dfm", help="DFM capability diagnostics")
    actions = parser.add_subparsers(dest="dfm_action", required=True)
    doctor = actions.add_parser("doctor", help="Check DFM config, workspace, and analyzers")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def collect_diagnostics() -> dict:
    try:
        config = load_dfm_config()
        config_report = {"valid": True, "values": {
            "runtime_python": config.runtime_python,
            "geometry_executable": config.geometry_executable,
            "geometry_timeout_seconds": config.geometry_timeout_seconds,
            "max_concurrent_runs": config.max_concurrent_runs,
            "timeout_seconds": config.timeout_seconds,
            "max_file_size_mb": config.max_file_size_mb,
            "max_pages": config.max_pages,
        }}
    except DFMError as exc:
        config_report = {"valid": False, "error": exc.to_dict()["error"]}

    workspace = DFMWorkspace()
    writable = False
    write_error = None
    probe: Path | None = None
    try:
        workspace.root.mkdir(parents=True, exist_ok=True)
        probe = workspace.root / f".doctor-{uuid4().hex}.tmp"
        probe.write_text("ok", encoding="utf-8")
        writable = probe.read_text(encoding="utf-8") == "ok"
    except OSError as exc:
        write_error = type(exc).__name__
    finally:
        if probe is not None:
            probe.unlink(missing_ok=True)

    context = AnalyzerContext("doctor", workspace.root, None, [])
    registry = build_default_registry(config if config_report["valid"] else None)
    capabilities = {key: registry.get(key).capability(context).to_dict() for key in registry.keys()}
    process_registry = build_default_process_registry()
    processes = {"supported": list(process_registry.keys())}
    for key in process_registry.keys():
        process_plan = process_registry.get(key).compile(context, {})
        processes[key] = {
            "adapter_version": process_plan.adapter_version,
            "scope_id": process_plan.scope_id,
            "scope_version": process_plan.scope_version,
        }
    step = registry.get("step")
    occt_cpp = registry.get("occt_cpp")
    dependencies = dependency_statuses(step.python_executable)
    html_report = _html_report_diagnostics()
    return {
        "ok": bool(config_report["valid"] and writable),
        "config": config_report,
        "workspace": {"path": str(workspace.root), "writable": writable, "error": write_error},
        "capabilities": capabilities,
        "html_report": html_report,
        "runtime": {
            "worker_import_path": step_worker.__name__,
            "worker_version": step_worker.WORKER_VERSION,
            "python_executable": step.python_executable,
            "dependencies": dependencies,
            "step_available": capabilities["step"]["status"] == "available",
            "occt_executable": occt_cpp.executable,
            "occt_engine_version": ENGINE_VERSION,
            "occt_available": capabilities["occt_cpp"]["status"] == "available",
        },
        "production_backend": {
            "backend_id": "external_occt_cpp",
            "status": capabilities["occt_cpp"]["status"],
            "connected": capabilities["occt_cpp"]["status"] == "available",
            "discovery_contract_version": DISCOVERY_SCHEMA_VERSION,
            "objective_contract_version": OBJECTIVE_SCHEMA_VERSION,
            "note": "The external OCCT CLI is experimental; PythonOCC remains the reference backend and NX remains optional.",
        },
        "processes": processes,
        "note": "Diagnostics never install CAD, OCR, or system dependencies.",
    }


def dfm_command(args) -> int:
    if getattr(args, "dfm_action", None) != "doctor":
        return 2
    report = collect_diagnostics()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"DFM workspace: {report['workspace']['path']}")
        print(f"Workspace writable: {report['workspace']['writable']}")
        print(f"Config valid: {report['config']['valid']}")
        print(
            f"STEP worker: {report['runtime']['worker_import_path']} "
            f"({report['runtime']['worker_version']})"
        )
        for dependency, available in report["runtime"]["dependencies"].items():
            print(f"{dependency} available: {available}")
        print(f"STEP capability available: {report['runtime']['step_available']}")
        print(
            "OCCT executable: "
            f"{report['runtime']['occt_executable'] or 'not found'} "
            f"({report['runtime']['occt_engine_version']}, experimental)"
        )
        print(f"OCCT capability available: {report['runtime']['occt_available']}")
        print(f"HTML report Agent Python: {report['html_report']['python_executable']}")
        print(
            "HTML report Playwright installed: "
            f"{report['html_report']['playwright_installed']}"
        )
        print(
            "HTML report Chromium usable: "
            f"{report['html_report']['chromium_usable']}"
        )
        if not report["html_report"]["ready"]:
            print(f"HTML report reason: {report['html_report']['reason']}")
        print(
            "Production geometry backend: "
            f"{report['production_backend']['backend_id']} "
            f"({report['production_backend']['status']})"
        )
        print(f"Supported processes: {', '.join(report['processes']['supported'])}")
        for key in report["processes"]["supported"]:
            process = report["processes"][key]
            print(
                f"{key}: adapter={process['adapter_version']} "
                f"scope={process['scope_id']}@{process['scope_version']}"
            )
        for key, capability in report["capabilities"].items():
            print(f"{key}: {capability['status']} - {capability['reason']}")
    return 0 if report["ok"] else 1
