"""Safe registration of opaque CAD and drawing inputs."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..config import DFMConfig
from ..contracts import InputRecord, ProjectManifest
from ..errors import DFMError
from .manifest import ManifestStore
from .step_preflight import inspect_step
from .parasolid_preflight import inspect_parasolid_xt
from .workspace import DFMWorkspace


_KINDS = {
    ".step": "step",
    ".stp": "step",
    ".pdf": "drawing",
    ".png": "drawing",
    ".jpg": "drawing",
    ".jpeg": "drawing",
    ".x_t": "parasolid",
}

_FORMAT_IDS = {
    "step": "step",
    "drawing": "drawing",
    "parasolid": "parasolid_xt",
}

_REPRESENTATIONS = {
    "step": "brep",
    "drawing": "document",
    "parasolid": "brep",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _input_mode(inputs: list[InputRecord]) -> str | None:
    kinds = {item.kind for item in inputs}
    geometry = kinds & {"step", "parasolid"}
    if geometry and "drawing" in kinds:
        return "fusion"
    if len(geometry) > 1:
        return "geometry"
    if "step" in geometry:
        return "step"
    if "parasolid" in geometry:
        return "parasolid"
    if "drawing" in kinds:
        return "drawing"
    return None


class InputRegistrar:
    def __init__(self, workspace: DFMWorkspace, config: DFMConfig) -> None:
        self.workspace = workspace
        self.config = config

    def register(self, project_id: str, source_path: str | Path) -> InputRecord:
        source = Path(source_path)
        if not source.is_file():
            raise DFMError(
                "input_not_found",
                f"DFM input does not exist: {source}",
                {"path": str(source)},
            )
        suffix = source.suffix.lower()
        kind = _KINDS.get(suffix)
        if kind is None:
            raise DFMError(
                "input_type_unsupported",
                "DFM input type is not supported.",
                {"suffix": suffix, "supported": sorted(_KINDS)},
            )
        size = source.stat().st_size
        limit = self.config.max_file_size_mb * 1024 * 1024
        if size > limit:
            raise DFMError(
                "input_too_large",
                "DFM input exceeds the configured size limit.",
                {"size_bytes": size, "limit_bytes": limit},
            )

        project_dir = self.workspace.project_dir(project_id)
        inputs_dir = project_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        temporary = inputs_dir / f".upload-{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        copied_bytes = 0
        try:
            with source.open("rb") as reader, temporary.open("wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    copied_bytes += len(chunk)
                    if copied_bytes > limit:
                        raise DFMError(
                            "input_too_large",
                            "DFM input exceeded the configured size limit while copying.",
                            {"size_bytes": copied_bytes, "limit_bytes": limit},
                        )
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            sha256 = digest.hexdigest()
            filename = f"input_{sha256[:16]}{suffix}"
            destination = inputs_dir / filename
            if destination.exists():
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

        try:
            if kind == "step":
                preflight = inspect_step(destination)
            elif kind == "parasolid":
                preflight = inspect_parasolid_xt(destination)
            else:
                preflight = {}
        except DFMError:
            # A rejected intake must not leave an unreferenced project input.
            destination.unlink(missing_ok=True)
            raise
        previous = ManifestStore(project_dir).load()
        superseded = next(
            (
                item
                for item in reversed(previous.inputs)
                if item.kind == kind and item.source_name == source.name
            ),
            None,
        )
        record = InputRecord(
            input_id=f"input_{kind}_{sha256[:16]}",
            kind=kind,
            source_name=source.name,
            relative_path=f"inputs/{filename}",
            size_bytes=copied_bytes,
            sha256=sha256,
            created_at=_utc_now(),
            preflight=preflight,
            supersedes_input_id=superseded.input_id if superseded else None,
            format_id=_FORMAT_IDS[kind],
            representation=_REPRESENTATIONS[kind],
        )
        selected: InputRecord = record

        def add(current: ProjectManifest) -> ProjectManifest:
            nonlocal selected
            for existing in current.inputs:
                if existing.sha256 == record.sha256 and existing.kind == record.kind:
                    selected = existing
                    return current
            inputs = [*current.inputs, record]
            return replace(
                current,
                inputs=inputs,
                input_mode=_input_mode(inputs),
                plans=[
                    replace(
                        plan,
                        status="invalidated",
                        invalidated_by=record.input_id,
                        affected_operation_ids=[item.operation_id for item in plan.operations],
                    )
                    for plan in current.plans
                ],
                updated_at=_utc_now(),
            )

        ManifestStore(project_dir).update(add)
        return selected
