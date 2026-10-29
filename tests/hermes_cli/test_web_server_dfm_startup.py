"""DFM startup version messages must work without remote synchronization."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def startup(monkeypatch):
    # Import after the hermetic-home fixture, including on Windows.
    import hermes_cli.web_server as web_server
    from tools.dfm.analyzers import occt
    from tools.dfm import config as config_module
    from tools.dfm import service as service_module

    config = config_module.DFMConfig(geometry_executable="configured-worker.exe")
    identity = SimpleNamespace(
        rule_set_code="test.rules",
        rule_set_version="installed-version",
        snapshot_id="installed-snapshot",
    )
    store = Mock()
    store.identity.return_value = identity
    get_service = Mock(return_value=SimpleNamespace(ontology_store=store))
    resolve = Mock(return_value="resolved-worker.exe")
    probe = Mock(return_value={
        "engine_version": "actual-worker-version",
        "contract_version": "actual-contract-version",
    })
    monkeypatch.setattr(config_module, "load_dfm_config", lambda: config)
    monkeypatch.setattr(service_module, "get_dfm_service", get_service)
    monkeypatch.setattr(occt, "discover_geometry_executable", resolve)
    monkeypatch.setattr(occt, "probe_geometry_executable", probe)
    return SimpleNamespace(
        config=config, store=store, get_service=get_service, resolve=resolve, probe=probe,
        web_server=web_server, config_module=config_module,
    )


@pytest.mark.parametrize("endpoint", ["", "https://dfm.example"])
def test_startup_prints_installed_rule_and_probed_worker_versions(
    startup, endpoint, monkeypatch, capsys, caplog
):
    monkeypatch.setattr(
        startup.config_module,
        "load_dfm_config",
        lambda: startup.config_module.DFMConfig(
            geometry_executable=startup.config.geometry_executable,
            ontology_endpoint=endpoint,
        ),
    )
    with caplog.at_level(logging.INFO, logger=startup.web_server.__name__):
        startup.web_server._start_dfm_ontology_sync()

    startup.get_service.assert_called_once_with()
    startup.resolve.assert_called_once_with(startup.config.geometry_executable)
    startup.probe.assert_called_once_with("resolved-worker.exe")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "version=installed-version, snapshot=installed-snapshot" in captured.err
    assert "version=actual-worker-version, contract=actual-contract-version" in captured.err
    assert "executable=resolved-worker.exe" in captured.err
    assert len(captured.err.splitlines()) == 2
    for line in captured.err.splitlines():
        assert line in caplog.messages


def test_missing_worker_is_reported_without_failing_startup(startup, capsys):
    startup.resolve.return_value = None

    startup.web_server._start_dfm_ontology_sync()

    startup.probe.assert_not_called()
    output = capsys.readouterr().err
    assert "version=installed-version" in output
    assert "OCCT Worker: unavailable (executable not found)" in output


def test_failed_worker_probe_preserves_rule_version_message(startup, capsys):
    startup.probe.side_effect = RuntimeError("worker handshake timed out")

    startup.web_server._start_dfm_ontology_sync()

    output = capsys.readouterr().err
    assert "version=installed-version" in output
    assert "OCCT Worker: unavailable (worker handshake timed out)" in output
    assert "version=actual-worker-version" not in output


def test_failed_rule_initialization_does_not_skip_worker_version(startup, capsys):
    startup.get_service.side_effect = RuntimeError("rule database unavailable")

    startup.web_server._start_dfm_ontology_sync()

    startup.probe.assert_called_once()
    output = capsys.readouterr().err
    assert "Current rule package: unavailable (rule database unavailable)" in output
    assert "version=actual-worker-version" in output


def test_invalid_config_is_reported_without_failing_startup(
    startup, monkeypatch, capsys
):
    def fail_config():
        raise RuntimeError("invalid DFM configuration")

    monkeypatch.setattr(startup.config_module, "load_dfm_config", fail_config)

    startup.web_server._start_dfm_ontology_sync()

    startup.get_service.assert_not_called()
    startup.probe.assert_not_called()
    assert "Startup versions unavailable: invalid DFM configuration" in capsys.readouterr().err
