"""Committed in-memory project state, durable deltas and atomic checkpoints."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from ..contracts import MANIFEST_SCHEMA_VERSION, ProjectManifest
from ..errors import DFMError
from . import journal


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}
_WINDOWS_REPLACE_ATTEMPTS = 6
_MAX_CACHED_PROJECTS = 32
_CHECKPOINT_COMMITS = 32
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CommittedState:
    manifest: ProjectManifest
    journal_end: int
    checkpoint_revision: int


_FileSignature = tuple[int, int, int, int]
_StateSignature = tuple[_FileSignature, _FileSignature | None]
# All ManifestStore instances in this process share the same committed state.
# The disk signature also detects atomic writes from another Hermes process.
_STATES: OrderedDict[Path, tuple[_StateSignature, _CommittedState]] = OrderedDict()


def _signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)


def _cached(path: Path, signature: _StateSignature) -> _CommittedState | None:
    with _LOCKS_GUARD:
        entry = _STATES.get(path)
        if entry is None or entry[0] != signature:
            return None
        _STATES.move_to_end(path)
        # Frozen dataclasses still contain mutable lists/dicts. Never lend the
        # canonical committed state to a caller that could mutate it in place.
        return deepcopy(entry[1])


def _remember(path: Path, signature: _StateSignature, state: _CommittedState) -> None:
    detached = deepcopy(state)
    with _LOCKS_GUARD:
        _STATES[path] = (signature, detached)
        _STATES.move_to_end(path)
        while len(_STATES) > _MAX_CACHED_PROJECTS:
            _STATES.popitem(last=False)


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(resolved, threading.RLock())


def _replace_manifest(source: Path, target: Path) -> None:
    """Tolerate short-lived Windows reader/AV locks without hiding real failures."""
    for attempt in range(_WINDOWS_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _WINDOWS_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.01 * (2**attempt))


class ManifestStore:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.path = self.project_dir / "project_manifest.json"
        self.journal_path = self.project_dir / "recovery" / "manifest.events.jsonl"
        self.lock_path = (
            self.project_dir.parent / ".locks" / f"{self.project_dir.name}.lock"
        )
        self._lock = _lock_for(self.path)

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, manifest: ProjectManifest) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self._process_lock():
                self._save_unlocked(manifest)

    def _save_unlocked(self, manifest: ProjectManifest) -> None:
        state = self._checkpoint_unlocked(_CommittedState(manifest, 0, manifest.revision))
        _remember(self.path, self._state_signature(), state)

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_manifest(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _checkpoint_unlocked(self, state: _CommittedState) -> _CommittedState:
        payload = json.dumps(state.manifest.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        self._write_atomic(self.path, payload.encode("utf-8"))
        # Checkpoint first: a crash before log rotation can safely skip old events.
        if self.journal_path.exists():
            self._write_atomic(self.journal_path, b"")
        return _CommittedState(state.manifest, 0, state.manifest.revision)

    def _state_signature(self) -> _StateSignature:
        return (_signature(self.path), _signature(self.journal_path) if self.journal_path.exists() else None)

    def checkpoint(self) -> ProjectManifest:
        """Seal a workflow boundary without creating another logical revision."""
        with self._lock:
            with self._process_lock():
                state = self._checkpoint_unlocked(self._load_state_unlocked())
                _remember(self.path, self._state_signature(), state)
                return state.manifest

    def load(self) -> ProjectManifest:
        with self._lock:
            try:
                cached = _cached(self.path, self._state_signature())
            except OSError as exc:
                raise DFMError("manifest_invalid", "The DFM project checkpoint is missing.", {"path": str(self.path)}) from exc
            if cached is not None:
                return cached.manifest
            with self._process_lock():
                return self._load_state_unlocked().manifest

    def _load_unlocked(self) -> ProjectManifest:
        return self._load_state_unlocked().manifest

    def _load_state_unlocked(self) -> _CommittedState:
        try:
            signature = self._state_signature()
            cached = _cached(self.path, signature)
            if cached is not None:
                return cached
            checkpoint = json.loads(self.path.read_text(encoding="utf-8"))
            payload, journal_end = journal.replay(self.journal_path, checkpoint)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DFMError(
                "manifest_invalid",
                "The DFM project manifest could not be read.",
                {"path": str(self.path)},
            ) from exc

        if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise DFMError(
                "manifest_version_unsupported",
                "The DFM project manifest uses an unsupported schema version.",
                {
                    "found": payload.get("schema_version"),
                    "supported": MANIFEST_SCHEMA_VERSION,
                },
            )
        try:
            manifest = ProjectManifest.from_dict(payload)
            state = _CommittedState(manifest, journal_end, int(checkpoint["revision"]))
            _remember(self.path, signature, state)
            return state
        except (KeyError, TypeError, ValueError) as exc:
            raise DFMError(
                "manifest_invalid",
                "The DFM project manifest has an invalid structure.",
                {"path": str(self.path)},
            ) from exc

    def update(
        self,
        transform: Callable[[ProjectManifest], ProjectManifest],
        expected_revision: int | None = None,
    ) -> ProjectManifest:
        with self._lock:
            with self._process_lock():
                state = self._load_state_unlocked()
                current = state.manifest
                if (
                    expected_revision is not None
                    and current.revision != expected_revision
                ):
                    raise DFMError(
                        "manifest_conflict",
                        "The DFM project changed since it was read.",
                        {"expected": expected_revision, "actual": current.revision},
                    )
                previous = current.to_dict()
                updated = transform(current)
                if updated.project_id != current.project_id:
                    raise DFMError(
                        "manifest_invalid", "Project id cannot change during update."
                    )
                updated = replace(updated, revision=current.revision + 1)
                event = journal.encode(previous, updated.to_dict())
                journal_end = journal.append(self.journal_path, event, state.journal_end)
                committed = _CommittedState(updated, journal_end, state.checkpoint_revision)
                if (updated.revision - state.checkpoint_revision >= _CHECKPOINT_COMMITS
                    or journal_end >= _MAX_JOURNAL_BYTES):
                    try:
                        committed = self._checkpoint_unlocked(committed)
                    except OSError:
                        # The event is already durable. Failed maintenance is not
                        # a failed user action; replay remains authoritative.
                        _LOG.warning("DFM checkpoint compaction deferred for %s", self.project_dir, exc_info=True)
                        committed = self._read_committed_unlocked()
                _remember(self.path, self._state_signature(), committed)
                return updated

    def _read_committed_unlocked(self) -> _CommittedState:
        with _LOCKS_GUARD:
            _STATES.pop(self.path, None)
        return self._load_state_unlocked()
