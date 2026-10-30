"""Durable geometry edit intents and semantic compare-and-swap commits."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Callable
from uuid import uuid4

from ..contracts import ProjectManifest
from ..errors import DFMError
from .manifest import ManifestStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def geometry_identity(manifest: ProjectManifest, input_sha256: str) -> str:
    """Progress, user notes and confirmations do not change geometric input."""
    superseded = {
        item.supersedes_input_id for item in manifest.inputs if item.supersedes_input_id
    }
    inputs = [
        (item.input_id, item.sha256)
        for item in manifest.inputs
        if item.input_id not in superseded and item.kind in {"step", "parasolid"}
    ]
    facts = {
        item.name: item.value
        for item in manifest.facts
        if item.status == "confirmed"
        and item.check_id is None
        and item.feature_id is None
        and item.name in {"process", "model_units", "pull_dir"}
    }
    metadata = (
        manifest.capabilities
        .get("geometry_discovery", {})
        .get("inputs", {})
        .get(input_sha256, {})
    )
    content = {
        "inputs": sorted(inputs),
        "process": manifest.process,
        "facts": facts,
        "geometry": {
            key: metadata.get(key)
            for key in (
                "worker_snapshot_id",
                "annotations",
                "relationships",
                "forming_scopes",
                "coverage",
            )
        },
    }
    return hashlib.sha256(
        json.dumps(
            content, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class GeometryEdit:
    store: ManifestStore
    manifest: ProjectManifest
    edit_id: str
    input_sha256: str
    base_identity: str

    @classmethod
    def begin(
        cls,
        store: ManifestStore,
        *,
        expected_revision: int,
        input_sha256: str,
        method: str,
        params: dict,
        expected_identity: str | None = None,
    ) -> "GeometryEdit":
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise DFMError(
                "manifest_revision_required",
                "Geometry editing requires the current project revision.",
            )
        if method not in {
            "features.revise",
            "forming_scopes.revise",
            "forming_scopes.analyze",
            "annotations.revise",
        } or not params.get("snapshot_id"):
            raise DFMError(
                "geometry_edit_invalid",
                "A geometry edit requires a supported method and base snapshot.",
            )
        edit_id = "edit_" + uuid4().hex
        payload = {
            "edit_id": edit_id,
            "input_sha256": input_sha256,
            "base_snapshot_id": params["snapshot_id"],
            "method": method,
            "params": params,
            "status": "pending",
            "created_at": _now(),
        }

        def record(current):
            if (
                expected_identity is not None
                and geometry_identity(current, input_sha256) != expected_identity
            ):
                raise DFMError(
                    "geometry_edit_conflict",
                    "The reviewed geometric state changed before editing.",
                )
            meta = (
                current.capabilities
                .get("geometry_discovery", {})
                .get("inputs", {})
                .get(input_sha256, {})
            )
            if meta.get("worker_snapshot_id") != params["snapshot_id"]:
                raise DFMError(
                    "geometry_edit_conflict",
                    "The requested geometry base is no longer current.",
                )
            return replace(
                current,
                geometry_edits=[*current.geometry_edits, payload],
                updated_at=_now(),
            )

        started = store.update(
            record,
            expected_revision=expected_revision if expected_identity is None else None,
        )
        return cls(
            store,
            started,
            edit_id,
            input_sha256,
            geometry_identity(started, input_sha256),
        )

    def commit(
        self,
        result_snapshot_id: str,
        transform: Callable[[ProjectManifest], ProjectManifest],
    ) -> ProjectManifest:
        def update(current):
            if geometry_identity(current, self.input_sha256) != self.base_identity:
                raise DFMError(
                    "geometry_edit_conflict",
                    "Geometric input changed while the edit was being prepared.",
                    {"edit_id": self.edit_id},
                )
            selected = next(
                (
                    item
                    for item in current.geometry_edits
                    if item["edit_id"] == self.edit_id
                ),
                None,
            )
            if selected is None or selected["status"] != "pending":
                raise DFMError(
                    "geometry_edit_conflict", "The geometry edit is not pending."
                )
            revised = transform(current)
            return replace(
                revised,
                geometry_edits=[
                    {
                        **item,
                        "status": "committed",
                        "result_snapshot_id": result_snapshot_id,
                        "completed_at": _now(),
                    }
                    if item["edit_id"] == self.edit_id
                    else item
                    for item in revised.geometry_edits
                ],
                updated_at=_now(),
            )

        return self.store.update(update)

    def assert_current(self) -> None:
        if (
            geometry_identity(self.store.load(), self.input_sha256)
            != self.base_identity
        ):
            raise DFMError(
                "geometry_edit_conflict",
                "Geometric input changed while the edit was being prepared.",
                {"edit_id": self.edit_id},
            )

    def fail(self, error: Exception) -> None:
        def update(current):
            return replace(
                current,
                geometry_edits=[
                    {
                        **item,
                        "status": "failed",
                        "error": getattr(error, "code", "geometry_edit_failed"),
                        "completed_at": _now(),
                    }
                    if item["edit_id"] == self.edit_id and item["status"] == "pending"
                    else item
                    for item in current.geometry_edits
                ],
                updated_at=_now(),
            )

        self.store.update(update)
