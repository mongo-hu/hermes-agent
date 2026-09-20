import argparse
import json
import subprocess

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import dfm as dfm_module
from hermes_cli.dfm import build_parser, collect_diagnostics, dfm_command
from tools.dfm.config import DFMConfig
from tools.dfm.workers.step_worker import WORKER_VERSION


def test_dfm_doctor_reports_workspace_config_and_capabilities(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dfm_module, "_html_report_diagnostics", lambda: {
        "python_executable": "test-python",
        "playwright_installed": True,
        "chromium_usable": True,
        "ready": True,
        "reason": "",
    })
    token = set_hermes_home_override(tmp_path / "home")
    try:
        report = collect_diagnostics()
        assert report["ok"] is True
        assert report["workspace"]["writable"] is True
        assert report["config"]["valid"] is True
        assert set(report["capabilities"]) == {
            "step",
            "occt_cpp",
            "parasolid",
            "drawing",
            "fusion",
        }
        assert report["capabilities"]["parasolid"]["status"] != "available"
        assert report["capabilities"]["drawing"]["status"] == "available"
        assert report["capabilities"]["drawing"]["details"]["applicable"] is False
        assert report["capabilities"]["fusion"]["status"] == "available"
        assert report["runtime"]["worker_import_path"] == "tools.dfm.workers.step_worker"
        assert report["runtime"]["worker_version"] == WORKER_VERSION
        assert set(report["runtime"]["dependencies"]) == {
            "pythonocc-core",
            "vtk",
        }
        assert all(
            isinstance(value, bool)
            for value in report["runtime"]["dependencies"].values()
        )
        assert report["runtime"]["step_available"] == (
            report["capabilities"]["step"]["status"] == "available"
        )
        assert report["html_report"]["ready"] is True
        assert report["html_report"]["python_executable"] == "test-python"
        geometry_backend = report["geometry_backend"]
        assert geometry_backend["backend_id"] == "occt_cpp_external"
        assert geometry_backend["analyzer_key"] == "occt_cpp"
        assert geometry_backend["status"] == report["capabilities"]["occt_cpp"]["status"]
        assert geometry_backend["connected"] is (
            report["capabilities"]["occt_cpp"]["status"] == "available"
        )
        assert geometry_backend["discovery_contract_version"] == 1
        assert geometry_backend["objective_contract_version"] == 4
        assert "adapter_task_schema_version" not in geometry_backend
        assert "experimental" in geometry_backend["note"]
        assert report["production_backend"] == geometry_backend
        assert set(report["processes"]["supported"]) == {
            "die_casting",
            "injection",
        }
        assert report["processes"]["injection"]["scope_id"] == (
            "injection.default"
        )
        assert report["processes"]["die_casting"]["scope_id"] == (
            "die_casting.topology-baseline"
        )

        code = dfm_command(argparse.Namespace(dfm_action="doctor", json=True))
        output = json.loads(capsys.readouterr().out)
        text_code = dfm_command(argparse.Namespace(dfm_action="doctor", json=False))
        text_output = capsys.readouterr().out
    finally:
        reset_hermes_home_override(token)

    assert code == 0
    assert text_code == 0
    assert output["workspace"]["writable"] is True
    assert output["html_report"]["chromium_usable"] is True
    assert "HTML report Playwright installed: True" in text_output
    assert "HTML report Chromium usable: True" in text_output
    assert "Configured geometry backend: occt_cpp_external [occt_cpp]" in text_output


def test_dfm_doctor_reports_the_configured_internal_pythonocc_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        dfm_module,
        "load_dfm_config",
        lambda: DFMConfig(geometry_backend="pythonocc_internal"),
    )
    monkeypatch.setattr(
        dfm_module,
        "_html_report_diagnostics",
        lambda: {
            "python_executable": "test-python",
            "playwright_installed": True,
            "chromium_usable": True,
            "ready": True,
            "reason": "",
        },
    )
    token = set_hermes_home_override(tmp_path / "home")
    try:
        report = collect_diagnostics()
    finally:
        reset_hermes_home_override(token)

    backend = report["geometry_backend"]
    assert backend["backend_id"] == "pythonocc_internal"
    assert backend["analyzer_key"] == "step"
    assert backend["status"] == report["capabilities"]["step"]["status"]
    assert "reference backend" in backend["note"]


def test_html_report_diagnostics_missing_playwright(monkeypatch):
    monkeypatch.setattr(dfm_module.importlib.util, "find_spec", lambda name: None)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("Chromium should not launch without Playwright")

    monkeypatch.setattr(dfm_module.subprocess, "run", unexpected_run)
    result = dfm_module._html_report_diagnostics()

    assert result["playwright_installed"] is False
    assert result["chromium_usable"] is False
    assert result["ready"] is False


def test_html_report_diagnostics_checks_chromium_launch(monkeypatch):
    monkeypatch.setattr(dfm_module.importlib.util, "find_spec", lambda name: object())
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "Browser executable missing")

    monkeypatch.setattr(dfm_module.subprocess, "run", probe)
    missing_browser = dfm_module._html_report_diagnostics()
    assert calls[0][0] == dfm_module.sys.executable
    assert missing_browser["playwright_installed"] is True
    assert missing_browser["chromium_usable"] is False
    assert missing_browser["ready"] is False
    assert "playwright install chromium" in missing_browser["reason"]

    monkeypatch.setattr(
        dfm_module.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    ready = dfm_module._html_report_diagnostics()
    assert ready["chromium_usable"] is True
    assert ready["ready"] is True


def test_dfm_parser_registers_doctor_subcommand():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    dfm_parser = build_parser(subparsers)
    dfm_parser.set_defaults(func=dfm_command)

    args = parser.parse_args(["dfm", "doctor", "--json"])

    assert args.dfm_action == "doctor"
    assert args.json is True


def test_dfm_is_a_builtin_cli_subcommand(tmp_path, monkeypatch):
    # The canonical test runner clears user-profile variables on Windows.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "dfm" in _BUILTIN_SUBCOMMANDS
