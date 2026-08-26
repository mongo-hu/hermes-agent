"""HTTP-only NX backend client; no local NX execution fallback exists."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from ...errors import DFMError
from ...contracts import ObjectiveArtifactManifest, ObjectiveResultManifest
from .contracts import NXCapability, NXJobStatus


@runtime_checkable
class NXBackendClient(Protocol):
    def capability(self) -> NXCapability: ...
    def submit(self, request: dict[str, Any], input_path: Path) -> NXJobStatus: ...
    def status(self, job_id: str) -> NXJobStatus: ...
    def cancel(self, job_id: str) -> NXJobStatus: ...
    def result(self, job_id: str) -> ObjectiveResultManifest: ...
    def download(
        self, job_id: str, artifact: ObjectiveArtifactManifest, target: BinaryIO
    ) -> None: ...


class HttpNXBackendClient:
    def __init__(self, endpoint: str, *, timeout_seconds: int = 30, token: str | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DFMError(
                "config_invalid",
                "dfm.nx.endpoint must be an absolute HTTP(S) URL.",
            )
        self.timeout_seconds = timeout_seconds
        self.token = token if token is not None else os.environ.get("NX_BACKEND_TOKEN", "")

    def capability(self) -> NXCapability:
        try:
            return NXCapability.from_dict(self._json("GET", "/v1/capabilities"))
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "nx_protocol_invalid",
                "NX service returned an invalid capability contract.",
            ) from exc

    def submit(self, request: dict[str, Any], input_path: Path) -> NXJobStatus:
        digest = self._sha256(input_path)
        if (
            request.get("input_sha256") != digest
            or request.get("input_format") != "parasolid_xt"
        ):
            raise DFMError(
                "objective_input_invalid",
                "NX objective task identity does not match the uploaded input.",
            )
        upload = self._json("POST", "/v1/inputs", {"sha256": digest, "size_bytes": input_path.stat().st_size, "filename": input_path.name})
        input_id = str(upload.get("input_id") or "")
        if not input_id:
            raise DFMError("nx_protocol_invalid", "NX service did not return input_id.")
        if bool(upload.get("upload_required", True)):
            self._upload(input_id, input_path)
        payload = {
            "schema_version": request.get("schema_version"),
            "input": {
                "input_id": input_id,
                "sha256": digest,
                "format_id": "parasolid_xt",
            },
            "task": dict(request),
        }
        return NXJobStatus.from_dict(self._json("POST", "/v1/jobs", payload))

    def status(self, job_id: str) -> NXJobStatus:
        return NXJobStatus.from_dict(self._json("GET", f"/v1/jobs/{quote(job_id)}"))

    def cancel(self, job_id: str) -> NXJobStatus:
        return NXJobStatus.from_dict(self._json("POST", f"/v1/jobs/{quote(job_id)}/cancel", {}))

    def result(self, job_id: str) -> ObjectiveResultManifest:
        payload = self._json("GET", f"/v1/jobs/{quote(job_id)}/result")
        try:
            return ObjectiveResultManifest.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "objective_protocol_invalid",
                "NX backend returned an invalid objective result manifest.",
            ) from exc

    def download(
        self, job_id: str, artifact: ObjectiveArtifactManifest, target: BinaryIO
    ) -> None:
        request = Request(self.endpoint + f"/v1/jobs/{quote(job_id)}/artifacts/{quote(artifact.artifact_id)}", headers=self._headers())
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    target.write(chunk)
        except OSError as exc:
            raise DFMError(
                "objective_backend_unavailable", "NX artifact download failed."
            ) from exc
        if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            raise DFMError(
                "objective_artifact_invalid",
                "NX artifact size or hash does not match its manifest.",
            )

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.endpoint + path, data=data, method=method, headers=self._headers(json_body=payload is not None))
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise DFMError(
                "objective_backend_unavailable",
                "NX backend request failed.",
                {"path": path},
            ) from exc
        if not isinstance(value, dict):
            raise DFMError("nx_protocol_invalid", "NX backend returned a non-object JSON response.")
        return value

    def _upload(self, input_id: str, path: Path) -> None:
        parsed = urlparse(self.endpoint)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=self.timeout_seconds)
        target = f"{parsed.path.rstrip('/')}/v1/inputs/{quote(input_id)}/content"
        headers = self._headers()
        headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
        try:
            connection.putrequest("PUT", target)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            response.read()
            if not 200 <= response.status < 300:
                raise DFMError(
                    "objective_backend_unavailable",
                    "NX input upload was rejected.",
                    {"status": response.status},
                )
        except OSError as exc:
            raise DFMError(
                "objective_backend_unavailable", "NX input upload failed."
            ) from exc
        finally:
            connection.close()

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
