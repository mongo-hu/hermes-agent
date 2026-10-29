"""Bounded small metadata for immutable shared files, never a mesh-object cache."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from threading import RLock

from ..errors import DFMError


SHARED_GEOMETRY_KINDS = frozenset({"preflight", "render_scene", "topology_map"})
_MAX_ENTRIES = 64
_MAX_METADATA_BYTES = 64 * 1024
_LOCK = RLock()
_CACHE: OrderedDict[tuple[str, str], tuple[tuple, "GeometryFileInfo"]] = OrderedDict()


@dataclass(frozen=True)
class GeometryFileInfo:
    size_bytes: int
    sha256: str
    _metadata_json: str

    @property
    def metadata(self) -> dict:
        # A caller can edit its projection without changing another reader.
        return json.loads(self._metadata_json)


def _stat_signature(value: os.stat_result) -> tuple:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _parse_header(content: bytes, kind: str) -> str:
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("Shared geometry JSON must contain an object")
    fields = {"schema_version", "run_id", "input_sha256"}
    fields.update(
        {
            "scene_id",
            "coordinate_system",
            "unit",
            "topology_snapshot_ref",
            "render_mesh_snapshot",
        }
        if kind == "render_scene"
        else {"map_id", "scene_ref", "topology_snapshot", "render_mesh_snapshot_ref"}
    )
    result = json.dumps(
        {key: value for key, value in payload.items() if key in fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(result.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("Shared geometry snapshot metadata exceeds the cache budget")
    return result


def _load_geometry_file(
    path: Path, kind: str, signature: tuple, previous: GeometryFileInfo | None
) -> GeometryFileInfo:
    header = kind in {"render_scene", "topology_map"}
    with path.open("rb") as stream:
        # Windows Path.stat and fstat can disagree on creation/change-time
        # semantics. Compare portable handle identity/size/mtime, and compare
        # the full path signature before/after reading separately.
        if _stat_signature(os.fstat(stream.fileno()))[:4] != signature[:4]:
            raise ValueError("Shared geometry changed before reading")
        if header and previous is None:
            content = stream.read()
            digest = hashlib.sha256(content).hexdigest()
            metadata = _parse_header(content, kind)
        else:
            hasher = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
            digest = hasher.hexdigest()
            if header and (previous is None or digest != previous.sha256):
                stream.seek(0)
                content = stream.read()
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError("Shared geometry changed while reading")
                metadata = _parse_header(content, kind)
            else:
                metadata = (
                    previous._metadata_json if header and previous is not None else "{}"
                )
        if (
            _stat_signature(os.fstat(stream.fileno()))[:4] != signature[:4]
            or _stat_signature(path.stat()) != signature
        ):
            raise ValueError("Shared geometry changed while reading")
    return GeometryFileInfo(signature[2], digest, metadata)


def inspect_geometry_file(
    path: Path, kind: str, *, force: bool = False
) -> GeometryFileInfo:
    """Reuse stable signatures during batches; force a full hash at publication.

    Signature reuse is an optimization for producer-owned immutable files, not
    content proof against forged file timestamps. Final sealing must use force.
    """
    if kind not in SHARED_GEOMETRY_KINDS:
        raise ValueError("Only shared geometry files may use the metadata cache")
    try:
        path = path.resolve(strict=True)
        key = os.path.normcase(str(path)), kind
        signature = _stat_signature(path.stat())
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None and cached[0] == signature and not force:
                _CACHE.move_to_end(key)
                return cached[1]
        info = _load_geometry_file(
            path, kind, signature, cached[1] if cached is not None else None
        )
        with _LOCK:
            _CACHE[key] = signature, info
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        return info
    except (OSError, ValueError, TypeError) as exc:
        raise DFMError(
            "geometry_artifact_invalid",
            "A shared geometry file cannot be read as a stable artifact.",
            {"path": str(path), "kind": kind},
        ) from exc


def clear_geometry_file_cache() -> None:
    with _LOCK:
        _CACHE.clear()
