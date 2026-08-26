import argparse
import json

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.dfm import build_parser, collect_diagnostics, dfm_command
from tools.dfm.workers.step_worker import WORKER_VERSION


def test_dfm_doctor_reports_workspace_config_and_capabilities(tmp_path, capsys):
    token = set_hermes_home_override(tmp_path / "home")
    try:
        report = collect_diagnostics()
        assert report["ok"] is True
        assert report["workspace"]["writable"] is True
        assert report["config"]["valid"] is True
        assert set(report["capabilities"]) == {
            "step",
            "occt",
            "parasolid",
            "drawing",
            "fusion",
        }
        assert report["capabilities"]["parasolid"]["status"] != "available"
        assert report["capabilities"]["drawing"]["status"] == "blocked"
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
        assert production_backend["status"] == report["capabilities"]["occt"]["status"]
        assert production_backend["connected"] is (
            report["capabilities"]["occt"]["status"] == "available"
        )
        assert production_backend["discovery_contract_version"] == 1
        assert production_backend["objective_contract_version"] == 4
        assert production_backend["adapter_task_schema_version"] == 2
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
