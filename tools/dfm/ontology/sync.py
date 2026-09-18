"""Background synchronization for immutable DFM publications."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
from typing import Any, Mapping
from uuid import uuid4

import requests
from ..config import DFMConfig
from ..errors import DFMError
from .store import LocalOntologyStore, _content_hash


logger = logging.getLogger(__name__)


class OntologySynchronizer:
    def __init__(
        self,
        *,
        store: LocalOntologyStore,
        root: Path,
        config: DFMConfig,
        http_client: Any = requests,
    ) -> None:
        self.store = store
        self.root = Path(root)
        self.config = config
        self.http = http_client
        self.packages_dir = self.root / "packages"
        self.revocations_path = self.root / "revocations.json"

    @staticmethod
    def _verify_hash(payload: Mapping[str, Any], expected_hash: str = "") -> None:
        """Require Artifact, metadata, and locally calculated SHA-256 to agree."""

        digest = _content_hash(payload)
        declared = str(payload.get("content_sha256") or "")
        if declared != digest:
            raise DFMError(
                "ontology_snapshot_invalid",
                "Downloaded DFM ontology content hash does not match.",
                {"declared": declared, "actual": digest},
            )
        if expected_hash and expected_hash != digest:
            raise DFMError(
                "ontology_snapshot_invalid",
                "DFM ontology service metadata and Artifact hash differ.",
                {"expected": expected_hash, "actual": digest},
            )

    @staticmethod
    def _json_response(response: Any) -> dict[str, Any]:
        try:
            response.raise_for_status()
            value = response.json()
        except Exception as exc:
            raise DFMError(
                "ontology_sync_failed", "DFM ontology service returned an invalid response."
            ) from exc
        if not isinstance(value, dict):
            raise DFMError("ontology_sync_failed", "DFM ontology response must be an object.")
        return value

    def _get_latest(self) -> dict[str, Any]:
        try:
            response = self.http.get(
                f"{self.config.ontology_endpoint}/v1/dfm/publications/latest",
                params={
                    "process": self.config.ontology_process,
                    "organization_id": self.config.ontology_organization_id,
                },
                timeout=self.config.ontology_request_timeout_seconds,
            )
        except Exception as exc:
            raise DFMError("ontology_sync_failed", "DFM ontology service is unavailable.") from exc
        return self._json_response(response)

    def _download(self, url: str) -> tuple[dict[str, Any], Mapping[str, str]]:
        try:
            response = self.http.get(
                url,
                timeout=self.config.ontology_request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise DFMError("ontology_sync_failed", "DFM ontology Artifact download failed.") from exc
        if not isinstance(payload, dict):
            raise DFMError("ontology_sync_failed", "DFM ontology Artifact must be a JSON object.")
        return payload, response.headers

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _archive(self, payload: Mapping[str, Any]) -> Path:
        snapshot_id = str(payload["snapshot_id"])
        package_path = self.packages_dir / f"{snapshot_id}.json"
        self._write_json(package_path, payload)
        return package_path

    def revoked_snapshot_ids(self) -> set[str]:
        if not self.revocations_path.is_file():
            return set()
        try:
            value = json.loads(self.revocations_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        return {str(item) for item in value if isinstance(item, str)} if isinstance(value, list) else set()

    def ensure_current_not_revoked(self) -> None:
        identity = self.store.identity()
        if identity.snapshot_id in self.revoked_snapshot_ids():
            raise DFMError(
                "ontology_snapshot_revoked",
                "The installed DFM ontology Snapshot has been revoked.",
                {"snapshot_id": identity.snapshot_id},
            )

    def rollback(self, snapshot_id: str) -> dict[str, Any]:
        if snapshot_id in self.revoked_snapshot_ids():
            raise DFMError(
                "ontology_snapshot_revoked",
                "A revoked DFM ontology Snapshot cannot be restored.",
                {"snapshot_id": snapshot_id},
            )
        package_path = self.packages_dir / f"{snapshot_id}.json"
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DFMError(
                "ontology_snapshot_missing",
                "The requested archived DFM ontology Snapshot is unavailable.",
                {"snapshot_id": snapshot_id},
            ) from exc
        self._verify_hash(payload)
        self.store.install_package(payload)
        return {"changed": True, "snapshot_id": snapshot_id, "rolled_back": True}

    def sync_once(self) -> dict[str, Any]:
        latest = self._get_latest()
        revoked = {str(item) for item in latest.get("revoked_snapshot_ids", [])}
        self._write_json(self.revocations_path, sorted(revoked))

        pinned = self.config.ontology_pinned_snapshot_id
        target_id = pinned or str(latest.get("snapshot_id") or "")
        if not target_id or target_id in revoked:
            raise DFMError(
                "ontology_snapshot_revoked",
                "The selected DFM ontology Snapshot is missing or revoked.",
                {"snapshot_id": target_id},
            )
        try:
            current = self.store.identity()
            if current.snapshot_id == target_id:
                return {"changed": False, "snapshot_id": target_id}
        except DFMError:
            pass

        if target_id == latest.get("snapshot_id"):
            artifact_url = str(latest.get("artifact_url") or "")
            expected_hash = str(latest.get("content_sha256") or "")
        else:
            artifact_url = (
                f"{self.config.ontology_endpoint}/v1/dfm/publications/{target_id}/artifact"
            )
            expected_hash = ""
        payload, headers = self._download(artifact_url)
        if str(payload.get("snapshot_id") or "") != target_id:
            raise DFMError("ontology_sync_failed", "Downloaded Snapshot identity is unexpected.")
        expected_hash = expected_hash or headers.get("X-DFM-Content-SHA256", "")
        self._verify_hash(payload, expected_hash)
        self._archive(payload)
        self.store.install_package(payload)
        return {"changed": True, "snapshot_id": target_id, "rolled_back": False}


class BackgroundOntologySync:
    def __init__(self, synchronizer: OntologySynchronizer, interval_seconds: int) -> None:
        self.synchronizer = synchronizer
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="dfm-ontology-sync", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.synchronizer.sync_once()
            except Exception:
                logger.exception("Background DFM ontology synchronization failed")
            self._stop.wait(self.interval_seconds)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
