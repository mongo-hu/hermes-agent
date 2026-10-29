import copy
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from tools.dfm.config import DFMConfig
from tools.dfm.errors import DFMError
from tools.dfm.ontology.store import LocalOntologyStore, _content_hash
from tools.dfm.ontology.sync import OntologySynchronizer


PACKAGE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "dfm"
    / "scopes"
    / "injection"
    / "ontology_snapshot_v2.json"
)


class Response:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return copy.deepcopy(self._payload)


class Client:
    def __init__(self, latest, artifact, headers=None):
        self.latest = latest
        self.artifact = artifact
        self.headers = headers or {}
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/latest"):
            return Response(self.latest)
        return Response(self.artifact, self.headers)


def package(snapshot_id):
    payload = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    payload["snapshot_id"] = snapshot_id
    payload["content_sha256"] = _content_hash(payload)
    return payload


def synchronizer(tmp_path, store, payload, revoked=None, expected_hash=None):
    latest = {
        "snapshot_id": payload["snapshot_id"],
        "content_sha256": expected_hash or payload["content_sha256"],
        "artifact_url": "https://dfm.example/v1/dfm/publications/current/artifact",
        "revoked_snapshot_ids": revoked or [],
    }
    config = DFMConfig(
        ontology_endpoint="https://dfm.example",
    )
    return OntologySynchronizer(
        store=store,
        root=tmp_path / "ontology",
        config=config,
        http_client=Client(latest, payload),
    )


def test_sync_verifies_hash_installs_and_archives(tmp_path, capsys, caplog):
    payload = package("ontology.test@2")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    sync = synchronizer(tmp_path, store, payload)

    with caplog.at_level(logging.INFO, logger="tools.dfm.ontology.sync"):
        result = sync.sync_once()

    assert result == {"changed": True, "snapshot_id": "ontology.test@2", "rolled_back": False}
    assert store.identity().snapshot_id == "ontology.test@2"
    assert (tmp_path / "ontology" / "packages" / "ontology.test@2.json").is_file()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[DFM] Rule package updated:" in captured.err
    assert "snapshot=not_installed -> ontology.test@2" in captured.err
    assert captured.err.count("\n") == 2
    assert "Rule service: available" in captured.err
    for line in captured.err.splitlines():
        assert line in caplog.messages


def test_sync_logs_installed_version_transition(tmp_path, capsys, caplog):
    previous = package("ontology.previous@1")
    payload = package("ontology.next@2")
    payload["rule_set"]["version"] = "next-version"
    payload["content_sha256"] = _content_hash(payload)
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    store.install_package(previous)
    sync = synchronizer(tmp_path, store, payload)

    with caplog.at_level(logging.INFO, logger="tools.dfm.ontology.sync"):
        result = sync.sync_once()

    assert result["changed"] is True
    assert store.identity().rule_set_version == payload["rule_set"]["version"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"rule_set={payload['rule_set']['rule_set_code']}" in captured.err
    assert f"version={previous['rule_set']['version']} -> next-version" in captured.err
    assert "snapshot=ontology.previous@1 -> ontology.next@2" in captured.err
    assert f"ontology_version={payload['ontology_version']}" in captured.err
    for line in captured.err.splitlines():
        assert line in caplog.messages


def test_sync_unchanged_package_does_not_log_an_update(tmp_path, capsys, caplog):
    payload = package("ontology.current@1")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    store.install_package(payload)
    sync = synchronizer(tmp_path, store, payload)

    with caplog.at_level(logging.INFO, logger="tools.dfm.ontology.sync"):
        result = sync.sync_once()

    assert result == {"changed": False, "snapshot_id": payload["snapshot_id"]}
    assert "Rule service: available" in capsys.readouterr().err
    assert not any("Rule package updated:" in item for item in caplog.messages)
    caplog.clear()
    sync.sync_once()
    assert capsys.readouterr().err == ""
    assert not caplog.records


def test_sync_failed_install_does_not_log_success(tmp_path, monkeypatch, capsys, caplog):
    payload = package("ontology.next@2")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    sync = synchronizer(tmp_path, store, payload)

    def fail_install(_payload):
        raise DFMError("ontology_snapshot_invalid", "Installation failed.")

    monkeypatch.setattr(store, "install_package", fail_install)
    with caplog.at_level(logging.INFO, logger="tools.dfm.ontology.sync"):
        with pytest.raises(DFMError):
            sync.sync_once()

    output = capsys.readouterr().err
    assert "Rule service: sync_failed" in output
    assert "Rule package updated:" not in output
    assert not any("Rule package updated:" in item for item in caplog.messages)


def test_sync_reports_http_error_once_and_recovery(tmp_path, capsys, caplog, monkeypatch):
    payload = package("ontology.current@1")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    store.install_package(payload)
    sync = synchronizer(tmp_path, store, payload)
    healthy_get = sync.http.get
    response = requests.Response()
    response.status_code = 503
    response.url = "https://dfm.example/v1/dfm/publications/latest"
    reason = "Unable to read MinIO object dfm-artifacts/ontology-snapshots/current.json."
    response._content = json.dumps({"detail": reason}).encode("utf-8")
    monkeypatch.setattr(sync.http, "get", lambda *_args, **_kwargs: response)

    with caplog.at_level(logging.INFO, logger="tools.dfm.ontology.sync"):
        with pytest.raises(DFMError) as exc_info:
            sync.sync_once()
        assert exc_info.value.code == "ontology_sync_failed"
        output = capsys.readouterr().err
        assert "Rule service: unavailable" in output
        assert "endpoint=https://dfm.example" in output
        assert "HTTP=503" in output
        assert reason in output
        assert output.strip() in caplog.messages
        assert store.identity().snapshot_id == payload["snapshot_id"]

        caplog.clear()
        with pytest.raises(DFMError):
            sync.sync_once()
        assert capsys.readouterr().err == ""
        assert not caplog.records

        monkeypatch.setattr(sync.http, "get", healthy_get)
        assert sync.sync_once()["changed"] is False
        output = capsys.readouterr().err
        assert "Rule service: recovered" in output
        assert f"snapshot={payload['snapshot_id']}" in output
        assert output.strip() in caplog.messages


def test_sync_reports_connection_timeout_without_replacing_rules(tmp_path, monkeypatch, capsys):
    payload = package("ontology.current@1")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    store.install_package(payload)
    sync = synchronizer(tmp_path, store, payload)

    def timeout(*_args, **_kwargs):
        raise requests.Timeout("connection timed out")

    monkeypatch.setattr(sync.http, "get", timeout)
    with pytest.raises(DFMError) as exc_info:
        sync.sync_once()

    assert exc_info.value.code == "ontology_sync_failed"
    output = capsys.readouterr().err
    assert "Rule service: unavailable" in output
    assert "connection timed out" in output
    assert store.identity().snapshot_id == payload["snapshot_id"]


def test_sync_rejects_metadata_and_artifact_hash_mismatch(tmp_path):
    payload = package("ontology.test@2")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    sync = synchronizer(tmp_path, store, payload, expected_hash="f" * 64)

    with pytest.raises(DFMError) as exc_info:
        sync.sync_once()

    assert exc_info.value.code == "ontology_snapshot_invalid"


def test_sync_uses_direct_http_requests(tmp_path):
    payload = package("ontology.test@2")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    sync = synchronizer(tmp_path, store, payload)

    result = sync.sync_once()

    assert result["snapshot_id"] == "ontology.test@2"
    assert len(sync.http.requests) == 2
    assert all(
        request_kwargs["proxies"] == {"http": "", "https": "", "all": ""}
        for _, request_kwargs in sync.http.requests
    )


def test_same_snapshot_is_revalidated_after_contract_upgrade(tmp_path):
    payload = package("ontology.test@stale")
    feature = next(
        item for item in payload["concepts"] if item["concept_type"] == "feature_type"
    )
    feature["properties"] = {"worker_kind": feature["properties"]["feature_kind"]}
    payload["content_sha256"] = _content_hash(payload)
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    with patch.object(LocalOntologyStore, "_validate_package"):
        store.install_package(payload)
    sync = synchronizer(tmp_path, store, payload)
    sync._archive(payload)

    with pytest.raises(DFMError) as exc_info:
        sync.sync_once()

    assert exc_info.value.code == "ontology_snapshot_invalid"


def test_revoked_installed_snapshot_blocks_runtime_without_safe_archive(tmp_path):
    payload = package("ontology.revoked@1")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    store.install_package(payload)
    sync = synchronizer(tmp_path, store, payload, revoked=["ontology.revoked@1"])
    sync._write_json(sync.revocations_path, ["ontology.revoked@1"])

    with pytest.raises(DFMError) as exc_info:
        sync.ensure_current_not_revoked()

    assert exc_info.value.code == "ontology_snapshot_revoked"
