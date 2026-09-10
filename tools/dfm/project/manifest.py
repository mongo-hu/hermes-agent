"""Atomic persistence for a DFM project manifest."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from ..contracts import MANIFEST_SCHEMA_VERSION, ProjectManifest
from ..errors import DFMError


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}
_WINDOWS_REPLACE_ATTEMPTS = 6


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
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / "project_manifest.json"
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
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_manifest(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> ProjectManifest:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> ProjectManifest:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
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
            return ProjectManifest.from_dict(payload)
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
                current = self._load_unlocked()
                if (
                    expected_revision is not None
                    and current.revision != expected_revision
                ):
                    raise DFMError(
                        "manifest_conflict",
                        "The DFM project changed since it was read.",
                        {"expected": expected_revision, "actual": current.revision},
                    )
                updated = transform(current)
                if updated.project_id != current.project_id:
                    raise DFMError(
                        "manifest_invalid", "Project id cannot change during update."
                    )
                updated = replace(updated, revision=current.revision + 1)
                self._save_unlocked(updated)
                return updated
