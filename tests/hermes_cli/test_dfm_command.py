import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.dfm import build_parser, collect_diagnostics, dfm_command
from tools.dfm.analyzers.occt import OcctAnalyzer
from tools.dfm.workers.step_worker import WORKER_VERSION


@pytest.fixture(autouse=True)
def isolated_cli_profile(tmp_path, monkeypatch):
    # CLI startup also resolves the default profile root, including in subprocesses.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))


def test_dfm_doctor_reports_workspace_config_and_capabilities(tmp_path, capsys):
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
        production_backend = report["production_backend"]
        assert production_backend["backend_id"] == "external_occt_cpp"
        assert production_backend["status"] == report["capabilities"]["occt_cpp"]["status"]
        assert production_backend["connected"] is (
            report["capabilities"]["occt_cpp"]["status"] == "available"
        )
        assert production_backend["discovery_contract_version"] == 1
        assert production_backend["objective_contract_version"] == 4
        assert "adapter_task_schema_version" not in production_backend
        assert "experimental" in production_backend["note"]
        assert "PythonOCC" in production_backend["note"]
        assert "NX" in production_backend["note"]
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
    finally:
        reset_hermes_home_override(token)

    assert code == 0
    assert output["workspace"]["writable"] is True


def test_dfm_parser_registers_doctor_subcommand():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    dfm_parser = build_parser(subparsers)
    dfm_parser.set_defaults(func=dfm_command)

    args = parser.parse_args(["dfm", "doctor", "--json"])

    assert args.dfm_action == "doctor"
    assert args.json is True


def test_dfm_is_a_builtin_cli_subcommand():
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "dfm" in _BUILTIN_SUBCOMMANDS


@pytest.mark.parametrize("engine_version", ["occt-dfm-geometry-9.8.7", "unavailable"])
def test_dfm_doctor_reports_analyzer_runtime_version(tmp_path, monkeypatch, engine_version):
    monkeypatch.setattr(OcctAnalyzer, "version", property(lambda self: engine_version))
    token = set_hermes_home_override(tmp_path / "home")
    try:
        report = collect_diagnostics()
    finally:
        reset_hermes_home_override(token)

    assert report["runtime"]["occt_engine_version"] == engine_version


@pytest.mark.parametrize("command", [["serve", "--help"], ["dfm", "doctor", "--help"]])
def test_cli_entry_point_loads_dfm_parser(tmp_path, monkeypatch, command):
    # Exercise the real startup imports used by the desktop backend.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "hermes_cli.main", *command],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()
