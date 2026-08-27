import copy
import json
from pathlib import Path

import pytest

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

    def get(self, url, **kwargs):
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


def test_sync_verifies_hash_installs_and_archives(tmp_path):
    payload = package("ontology.test@2")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    sync = synchronizer(tmp_path, store, payload)

    result = sync.sync_once()

    assert result == {"changed": True, "snapshot_id": "ontology.test@2", "rolled_back": False}
    assert store.identity().snapshot_id == "ontology.test@2"
    assert (tmp_path / "ontology" / "packages" / "ontology.test@2.json").is_file()


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


def test_revoked_installed_snapshot_blocks_runtime_without_safe_archive(tmp_path):
    payload = package("ontology.revoked@1")
    store = LocalOntologyStore(tmp_path / "dfm-ontology.sqlite3")
    store.install_package(payload)
    sync = synchronizer(tmp_path, store, payload, revoked=["ontology.revoked@1"])
    sync._write_json(sync.revocations_path, ["ontology.revoked@1"])

    with pytest.raises(DFMError) as exc_info:
        sync.ensure_current_not_revoked()

    assert exc_info.value.code == "ontology_snapshot_revoked"
