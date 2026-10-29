"""Bounded in-memory working projections of small G/evaluation documents.

Files remain the recovery/artifact representation. Cache hits never authorize
publication: the existing fresh artifact hash gates still apply at sealing.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4


SMALL_RESULT_KINDS = frozenset({"geometric_results", "evaluations"})
_ROW_KEYS = {"geometric_results": "results", "evaluations": "evaluations"}
_MAX_ENTRIES = 32
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_ENCODED_BYTES = 16 * 1024 * 1024
_LOCK = RLock()
_CACHE: OrderedDict[tuple[str, str], "_Entry"] = OrderedDict()
_ENCODED_BYTES = 0


@dataclass(frozen=True)
class _Entry:
    signature: tuple
    size_bytes: int
    sha256: str
    payload: dict


def _signature(value: os.stat_result) -> tuple:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _key(path: Path, kind: str) -> tuple[str, str]:
    if kind not in SMALL_RESULT_KINDS:
        raise ValueError("Only small result/evaluation documents may use this cache")
    return os.path.normcase(str(path)), kind


def _decode(raw: bytes, kind: str) -> dict:
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON result value: {value}")

    payload = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(payload, dict) or not isinstance(
        payload.get(_ROW_KEYS[kind]), list
    ):
        raise ValueError("Result document must contain its declared record list")
    return payload


def _forget(key: tuple[str, str]) -> None:
    global _ENCODED_BYTES
    with _LOCK:
        previous = _CACHE.pop(key, None)
        if previous is not None:
            _ENCODED_BYTES -= previous.size_bytes


def _remember(key: tuple[str, str], entry: _Entry) -> bool:
    global _ENCODED_BYTES
    with _LOCK:
        _forget(key)
        if (
            entry.size_bytes > min(_MAX_DOCUMENT_BYTES, _MAX_ENCODED_BYTES)
            or _MAX_ENTRIES < 1
        ):
            return False
        _CACHE[key] = entry
        _ENCODED_BYTES += entry.size_bytes
        while len(_CACHE) > _MAX_ENTRIES or _ENCODED_BYTES > _MAX_ENCODED_BYTES:
            _old_key, removed = _CACHE.popitem(last=False)
            _ENCODED_BYTES -= removed.size_bytes
        return True


def _read_entry(path: Path, kind: str, signature: tuple) -> _Entry:
    with path.open("rb") as stream:
        # Windows Path.stat/fstat may use different creation/change-time rules.
        if _signature(os.fstat(stream.fileno()))[:4] != signature[:4]:
            raise ValueError("Result document changed before reading")
        raw = stream.read()
        if _signature(os.fstat(stream.fileno()))[:4] != signature[:4]:
            raise ValueError("Result document changed while reading")
    if _signature(path.stat()) != signature or len(raw) != signature[2]:
        raise ValueError("Result document was replaced while reading")
    return _Entry(
        signature, len(raw), hashlib.sha256(raw).hexdigest(), _decode(raw, kind)
    )


def read_result_document(
    path: Path,
    kind: str,
    *,
    sha256: str = "",
    size_bytes: int = 0,
    force: bool = False,
) -> dict:
    """Return a private JSON-equivalent projection, never a mesh/field cache.

    A force read checks actual content even when timestamps have been forged.
    Callers consuming registered artifacts provide their expected digest/size.
    """
    path = path.resolve(strict=True)
    key = _key(path, kind)
    try:
        signature = _signature(path.stat())
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None and cached.signature == signature and not force:
                if (not sha256 or sha256 == cached.sha256) and size_bytes in {
                    0,
                    cached.size_bytes,
                }:
                    _CACHE.move_to_end(key)
                else:
                    cached = None
            else:
                cached = None
        if cached is not None:
            # The private entry is immutable; cloning need not block other runs.
            return deepcopy(cached.payload)
        entry = _read_entry(path, kind, signature)
        if (sha256 and sha256 != entry.sha256) or size_bytes not in {
            0,
            entry.size_bytes,
        }:
            raise ValueError("Result document descriptor does not match its content")
        retained = _remember(key, entry)
        return deepcopy(entry.payload) if retained else entry.payload
    except (OSError, ValueError, TypeError):
        _forget(key)
        raise


def write_result_document(path: Path, kind: str, payload: dict) -> tuple[int, str]:
    """Atomically materialize a working table and warm its canonical projection."""
    path = path.resolve()
    key = _key(path, kind)
    if not isinstance(payload, dict) or not isinstance(
        payload.get(_ROW_KEYS[kind]), list
    ):
        raise ValueError("Result document must contain its declared record list")
    raw = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode(
        "utf-8"
    )
    digest = hashlib.sha256(raw).hexdigest()
    retain = (
        len(raw) <= min(_MAX_DOCUMENT_BYTES, _MAX_ENCODED_BYTES) and _MAX_ENTRIES > 0
    )
    # Normalize tuples/JSON numeric forms exactly as a cold restart would read.
    normalized = _decode(raw, kind) if retain else None
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    created = False
    try:
        with temporary.open("xb") as stream:
            created = True
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            written_identity = _signature(os.fstat(stream.fileno()))[:4]
        os.replace(temporary, path)
        created = False
        signature = _signature(path.stat())
        if normalized is not None and signature[:4] == written_identity:
            _remember(key, _Entry(signature, len(raw), digest, normalized))
        else:
            _forget(key)
        return len(raw), digest
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def clear_result_document_cache() -> None:
    global _ENCODED_BYTES
    with _LOCK:
        _CACHE.clear()
        _ENCODED_BYTES = 0
