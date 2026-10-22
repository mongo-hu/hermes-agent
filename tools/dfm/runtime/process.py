"""Persistent ``dfm.geometry/v2`` client and project-owned session pool.

The public boundary deliberately knows only snapshots, features, forming scopes,
Geometric IDs, jobs, and evidence.  Worker operation/calculator/quantity routing
is not represented here and cannot leak into ordinary geometry requests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import ctypes
import hashlib
import json
import os
import queue
import re
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from hermes_cli._subprocess_compat import windows_hide_flags

from ..analyzers.base import CancellationToken
from ..contracts import WorkerEvent
from ..errors import DFMError
from .events import parse_worker_event


SESSION_CONTRACT = "dfm.geometry/v2"
SESSION_SCHEMA_BUNDLE_VERSION = "2.0.0"
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})
_RECOVERABLE_SNAPSHOT_METHODS = frozenset({
    "features.discover",
    "features.revise",
    "forming_scopes.analyze",
    "forming_scopes.revise",
    "snapshots.merge",
})
_ASYNC_SNAPSHOT_METHODS = frozenset({"features.discover", "forming_scopes.analyze"})


def geometry_capability_identity(capability: Mapping[str, Any]) -> str:
    """Return the build identity pinned by plans and recovery records."""

    values = (
        str(capability.get("schema_bundle_version") or ""),
        str(capability.get("engine_version") or ""),
        str(capability.get("dependency_fingerprint") or ""),
    )
    if not all(values) or re.fullmatch(r"[0-9a-f]{64}", values[2]) is None:
        raise DFMError(
            "geometry_capability_identity_invalid",
            "The OCCT geometry Worker build identity is incomplete.",
        )
    return ":".join(values)


def _snapshot_state_sha256(payload: Mapping[str, Any]) -> str:
    """Fingerprint the semantic state recreated by a snapshot mutation."""

    state: dict[str, list[Any]] = {}
    for key in ("features", "forming_scopes", "coverage"):
        if key not in payload:
            continue
        values = payload.get(key)
        if not isinstance(values, list):
            raise DFMError(
                "geometry_protocol_invalid",
                "A snapshot-producing Worker method returned invalid semantic state.",
                {"field": key},
            )
        state[key] = sorted(
            values,
            key=lambda value: json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if not state:
        raise DFMError(
            "geometry_protocol_invalid",
            "A snapshot-producing Worker method omitted its semantic state.",
        )
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resident_bytes(pid: int) -> int:
    """Best-effort resident-set accounting for enforcing the configured pool budget."""

    if os.name == "nt":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        handle = kernel32.OpenProcess(0x1000 | 0x0400, False, pid)
        if not handle:
            return 0
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return 0
            return int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)
    statm = Path(f"/proc/{pid}/statm")
    try:
        pages = int(statm.read_text(encoding="ascii").split()[1])
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError, AttributeError):
        return 0


class GeometrySession:
    """One persistent JSON-RPC connection to one geometry Worker process."""

    def __init__(
        self,
        executable: str,
        *,
        request_timeout_seconds: float,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        if not executable:
            raise DFMError(
                "geometry_dependency_missing",
                "The configured OCCT geometry executable is unavailable.",
            )
        self.executable = str(Path(executable).resolve())
        self.request_timeout_seconds = request_timeout_seconds
        self._on_disconnect = on_disconnect
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Queue[dict[str, Any]]] = {}
        self._job_event_lock = threading.Lock()
        self._job_event_callbacks: dict[
            str, Callable[[dict[str, Any]], None]
        ] = {}
        self._stderr_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=200)
        self._next_request_id = 0
        self._closed = threading.Event()
        self._disconnect_reason = ""
        try:
            self._process = subprocess.Popen(
                [self.executable, "serve", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=windows_hide_flags(),
            )
        except OSError as exc:
            raise DFMError(
                "geometry_engine_unhealthy",
                "The OCCT geometry session could not start.",
                {"executable": self.executable},
            ) from exc
        self._reader = threading.Thread(
            target=self._read_responses,
            name="dfm-geometry-rpc",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name="dfm-geometry-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        capability = self.call("capabilities.get", {})
        if capability.get("contract_version") != SESSION_CONTRACT:
            self.close()
            raise DFMError(
                "geometry_protocol_invalid",
                "The OCCT geometry Worker does not provide dfm.geometry/v2.",
                {"contract_version": capability.get("contract_version")},
            )
        if capability.get("schema_bundle_version") != SESSION_SCHEMA_BUNDLE_VERSION:
            self.close()
            raise DFMError(
                "geometry_schema_bundle_invalid",
                "The OCCT geometry Worker schema bundle is not the pinned v2 release.",
                {
                    "expected": SESSION_SCHEMA_BUNDLE_VERSION,
                    "actual": capability.get("schema_bundle_version"),
                },
            )
        if capability.get("backend") != "analysis_situs+occt":
            self.close()
            raise DFMError(
                "geometry_backend_invalid",
                "The geometry Worker does not provide the required Analysis Situs + OCCT backend.",
            )
        try:
            geometry_capability_identity(capability)
        except DFMError:
            self.close()
            raise
        methods = capability.get("methods")
        required_methods = {
            "snapshots.open",
            "snapshots.renew",
            "snapshots.close",
            "features.discover",
            "features.revise",
            "forming_scopes.analyze",
            "forming_scopes.revise",
            "snapshots.merge",
            "geometrics.compute",
            "jobs.status",
            "jobs.cancel",
        }
        if not isinstance(methods, Mapping) or any(
            methods.get(name) != "available" for name in required_methods
        ):
            self.close()
            raise DFMError(
                "geometry_capability_missing",
                "The OCCT geometry Worker lacks a required v2 session method.",
                {"required_methods": sorted(required_methods)},
            )
        self.capability = capability

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def alive(self) -> bool:
        return not self._closed.is_set() and self._process.poll() is None

    @property
    def stderr_tail(self) -> list[str]:
        with self._stderr_lock:
            return list(self._stderr_tail)

    def _read_responses(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._disconnect("Worker stdout contained non-JSON protocol data.")
                    return
                request_id = message.get("id") if isinstance(message, dict) else None
                if not isinstance(request_id, int):
                    params = (
                        message.get("params") if isinstance(message, dict) else None
                    )
                    job_id = (
                        str(params.get("job_id") or "")
                        if isinstance(params, Mapping)
                        else ""
                    )
                    if (
                        isinstance(message, dict)
                        and message.get("method") == "job.event"
                        and job_id
                    ):
                        with self._job_event_lock:
                            callback = self._job_event_callbacks.get(job_id)
                        if callback is not None:
                            try:
                                callback(dict(params))
                            except Exception:
                                # Polling invokes the same callback on the run
                                # thread, where failures can be handled without
                                # killing the shared protocol reader.
                                pass
                        continue
                    self._disconnect("Worker response has an invalid request ID.")
                    return
                with self._pending_lock:
                    waiter = self._pending.pop(request_id, None)
                if waiter is None:
                    self._disconnect("Worker returned an unknown request ID.")
                    return
                waiter.put(message)
        finally:
            if not self._closed.is_set():
                self._disconnect("The OCCT geometry Worker connection ended.")

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            with self._stderr_lock:
                self._stderr_tail.append(line.rstrip("\r\n"))

    def _disconnect(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self._disconnect_reason = reason
        self._closed.set()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        failure = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32000,
                "message": "Server error",
                "data": {"code": "geometry_session_lost", "detail": reason},
            },
        }
        for waiter in pending:
            waiter.put(failure)
        if self._on_disconnect is not None:
            self._on_disconnect(reason)

    def call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self.alive:
            raise DFMError(
                "geometry_session_lost",
                self._disconnect_reason or "The OCCT geometry session is closed.",
                {"stderr_tail": self.stderr_tail},
            )
        waiter: Queue[dict[str, Any]] = Queue(maxsize=1)
        with self._pending_lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            self._pending[request_id] = waiter
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        try:
            with self._write_lock:
                if not self.alive or self._process.stdin is None:
                    raise BrokenPipeError
                self._process.stdin.write(
                    json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._disconnect("The OCCT geometry Worker request pipe failed.")
            raise DFMError(
                "geometry_session_lost",
                self._disconnect_reason,
                {"stderr_tail": self.stderr_tail},
            ) from exc
        try:
            response = waiter.get(
                timeout=(
                    self.request_timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                )
            )
        except Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise DFMError(
                "geometry_request_timeout",
                "The OCCT geometry Worker did not answer the session request in time.",
                {"method": method},
            ) from exc
        error = response.get("error")
        if isinstance(error, Mapping):
            data = error.get("data")
            details = dict(data) if isinstance(data, Mapping) else {}
            raise DFMError(
                str(details.pop("code", "geometry_worker_error")),
                str(
                    details.pop(
                        "detail", error.get("message") or "Worker request failed."
                    )
                ),
                details,
            )
        result = response.get("result")
        if (
            not isinstance(result, dict)
            or result.get("contract_version") != SESSION_CONTRACT
        ):
            raise DFMError(
                "geometry_protocol_invalid",
                "The OCCT geometry Worker returned an invalid v2 response.",
                {"method": method},
            )
        return result

    def wait_job(
        self,
        job_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
        cancellation: Any | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        renew: Callable[[], None] | None = None,
        renew_interval_seconds: float = 30.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        next_renewal = time.monotonic() + max(1.0, renew_interval_seconds)
        cancel_sent = False
        if on_progress is not None:
            with self._job_event_lock:
                self._job_event_callbacks[job_id] = on_progress
        try:
            while True:
                if (
                    cancellation is not None
                    and cancellation.is_cancelled
                    and not cancel_sent
                ):
                    self.call("jobs.cancel", {"job_id": job_id})
                    cancel_sent = True
                now = time.monotonic()
                if renew is not None and now >= next_renewal:
                    renew()
                    next_renewal = now + max(1.0, renew_interval_seconds)
                status = self.call("jobs.status", {"job_id": job_id})
                if on_progress is not None:
                    on_progress(status)
                state = status.get("status")
                if state in TERMINAL_JOB_STATES:
                    if state == "completed" and isinstance(status.get("result"), dict):
                        return status["result"]
                    error = status.get("error")
                    if state == "cancelled":
                        raise DFMError("run_cancelled", "The DFM run was cancelled.")
                    raise DFMError(
                        str(
                            error.get("code")
                            if isinstance(error, Mapping)
                            else "geometry_job_failed"
                        ),
                        str(
                            error.get("detail")
                            if isinstance(error, Mapping)
                            else "The geometry job failed."
                        ),
                        {"job_id": job_id},
                    )
                if now >= deadline:
                    self.call("jobs.cancel", {"job_id": job_id})
                    raise DFMError(
                        "geometry_operation_timeout",
                        "The OCCT geometry job exceeded its timeout.",
                        {"job_id": job_id, "timeout_seconds": timeout_seconds},
                    )
                time.sleep(poll_interval_seconds)
        finally:
            with self._job_event_lock:
                self._job_event_callbacks.pop(job_id, None)

    def close(self) -> None:
        if self._closed.is_set() and self._process.poll() is not None:
            return
        self._closed.set()
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._reader.join(timeout=1)
        self._stderr_reader.join(timeout=1)


@dataclass
class ProjectGeometrySession:
    project_id: str
    session: GeometrySession
    snapshot_ids: set[str] = field(default_factory=set)
    snapshots_by_input_sha256: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_by_snapshot_id: dict[str, str] = field(default_factory=dict)
    snapshot_aliases: dict[str, str] = field(default_factory=dict)
    pending_snapshot_jobs: dict[str, tuple[str, dict[str, Any], str]] = field(
        default_factory=dict
    )
    job_snapshot_ids: dict[str, str] = field(default_factory=dict)
    active_job_ids: set[str] = field(default_factory=set)
    snapshot_lease_seconds: dict[str, int] = field(default_factory=dict)
    snapshot_last_renewed: dict[str, float] = field(default_factory=dict)
    reference_ids: set[str] = field(default_factory=set)
    close_requested: bool = False
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class SnapshotRecoveryState:
    input_path: str
    input_sha256: str
    artifact_dir: str
    lease_seconds: int
    opened_snapshot_id: str
    current_snapshot_id: str
    schema_bundle_version: str
    engine_version: str
    dependency_fingerprint: str
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.artifact_dir) / "session-recovery.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": SESSION_CONTRACT,
            "input_path": self.input_path,
            "input_sha256": self.input_sha256,
            "artifact_dir": self.artifact_dir,
            "lease_seconds": self.lease_seconds,
            "opened_snapshot_id": self.opened_snapshot_id,
            "current_snapshot_id": self.current_snapshot_id,
            "schema_bundle_version": self.schema_bundle_version,
            "engine_version": self.engine_version,
            "dependency_fingerprint": self.dependency_fingerprint,
            "steps": list(self.steps),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SnapshotRecoveryState":
        if payload.get("contract_version") != SESSION_CONTRACT:
            raise ValueError("recovery contract does not match dfm.geometry/v2")
        steps = payload.get("steps")
        if not isinstance(steps, list) or any(
            not isinstance(item, dict) for item in steps
        ):
            raise ValueError("recovery steps are invalid")
        state = cls(
            input_path=str(payload["input_path"]),
            input_sha256=str(payload["input_sha256"]),
            artifact_dir=str(payload["artifact_dir"]),
            lease_seconds=int(payload["lease_seconds"]),
            opened_snapshot_id=str(payload["opened_snapshot_id"]),
            current_snapshot_id=str(payload["current_snapshot_id"]),
            schema_bundle_version=str(payload["schema_bundle_version"]),
            engine_version=str(payload["engine_version"]),
            dependency_fingerprint=str(payload["dependency_fingerprint"]),
            steps=[dict(item) for item in steps],
        )
        if (
            not state.opened_snapshot_id
            or not state.current_snapshot_id
            or len(state.input_sha256) != 64
        ):
            raise ValueError("recovery identity is incomplete")
        try:
            geometry_capability_identity(state.to_dict())
        except DFMError as exc:
            raise ValueError("recovery build identity is incomplete") from exc
        return state


class GeometrySessionPool:
    """Own at most one Worker connection per project and never cross-route snapshots."""

    def __init__(
        self,
        executable: str,
        *,
        request_timeout_seconds: float,
        job_timeout_seconds: float,
        poll_interval_seconds: float,
        idle_timeout_seconds: float,
        max_sessions: int,
        memory_budget_mb: int,
    ) -> None:
        self.executable = executable
        self.request_timeout_seconds = request_timeout_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_sessions = max_sessions
        self.memory_budget_bytes = memory_budget_mb * 1024 * 1024
        self._lock = threading.RLock()
        self._projects: dict[str, ProjectGeometrySession] = {}
        self._references: dict[str, set[str]] = {}
        self._recovery: dict[str, dict[str, SnapshotRecoveryState]] = {}
        self._maintenance_stop = threading.Event()
        self._maintenance = threading.Thread(
            target=self._maintain_snapshot_leases,
            name="dfm-geometry-leases",
            daemon=True,
        )
        self._maintenance.start()

    def _maintain_snapshot_leases(self) -> None:
        while not self._maintenance_stop.wait(5.0):
            now = time.monotonic()
            with self._lock:
                candidates = [
                    (owned, snapshot_id, lease_seconds)
                    for owned in self._projects.values()
                    if owned.session.alive
                    and (
                        owned.reference_ids
                        or owned.active_job_ids
                        or now - owned.last_used < self.idle_timeout_seconds
                    )
                    for snapshot_id, lease_seconds in (
                        owned.snapshot_lease_seconds.items()
                    )
                    if now - owned.snapshot_last_renewed.get(snapshot_id, 0.0)
                    >= max(1.0, lease_seconds / 3)
                ]
            for owned, snapshot_id, lease_seconds in candidates:
                try:
                    owned.session.call(
                        "snapshots.renew",
                        {
                            "snapshot_id": snapshot_id,
                            "lease_seconds": lease_seconds,
                        },
                    )
                except DFMError as exc:
                    if exc.code not in {
                        "snapshot_not_found",
                        "geometry_session_lost",
                    }:
                        continue
                    with self._lock:
                        owned.snapshot_ids.discard(snapshot_id)
                        owned.snapshot_lease_seconds.pop(snapshot_id, None)
                        owned.snapshot_last_renewed.pop(snapshot_id, None)
                else:
                    with self._lock:
                        owned.snapshot_last_renewed[snapshot_id] = time.monotonic()
            with self._lock:
                self._evict_idle()
                self._evict_for_memory()

    @staticmethod
    def _write_recovery(state: SnapshotRecoveryState) -> None:
        state.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(state.path)

    def _load_recovery(
        self,
        project_id: str,
        *,
        input_path: Path,
        input_sha256: str,
        artifact_dir: Path,
        lease_seconds: int,
    ) -> SnapshotRecoveryState | None:
        by_input = self._recovery.setdefault(project_id, {})
        existing = by_input.get(input_sha256)
        if existing is not None:
            return existing
        path = artifact_dir / "session-recovery.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not {
                "schema_bundle_version",
                "engine_version",
                "dependency_fingerprint",
            }.issubset(payload):
                return None
            state = SnapshotRecoveryState.from_dict(payload)
            if any(
                re.fullmatch(
                    r"[0-9a-f]{64}", str(step.get("result_state_sha256") or "")
                )
                is None
                for step in state.steps
            ):
                return None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise DFMError(
                "geometry_recovery_invalid",
                "The persisted geometry session recovery log is invalid.",
                {"path": str(path)},
            ) from exc
        if (
            Path(state.input_path).resolve() != input_path.resolve()
            or state.input_sha256 != input_sha256
            or Path(state.artifact_dir).resolve() != artifact_dir.resolve()
        ):
            raise DFMError(
                "geometry_recovery_invalid",
                "The geometry recovery log belongs to another model or project.",
                {"path": str(path)},
            )
        state.lease_seconds = lease_seconds
        by_input[input_sha256] = state
        return state

    @staticmethod
    def _logical_snapshot_id(
        owned: ProjectGeometrySession, live_snapshot_id: str
    ) -> str:
        return next(
            (
                logical
                for logical, live in owned.snapshot_aliases.items()
                if live == live_snapshot_id
            ),
            live_snapshot_id,
        )

    def _record_recovery_step(
        self,
        project_id: str,
        owned: ProjectGeometrySession,
        method: str,
        params: Mapping[str, Any],
        input_sha256: str,
        result: Mapping[str, Any],
    ) -> None:
        state = self._recovery.get(project_id, {}).get(input_sha256)
        result_snapshot_id = str(result.get("snapshot_id") or "")
        if state is None or not result_snapshot_id:
            return
        logical_params = json.loads(json.dumps(dict(params)))
        for key in ("snapshot_id", "base_snapshot_id"):
            if isinstance(logical_params.get(key), str):
                logical_params[key] = self._logical_snapshot_id(
                    owned, logical_params[key]
                )
        if isinstance(logical_params.get("snapshot_ids"), list):
            logical_params["snapshot_ids"] = [
                self._logical_snapshot_id(owned, str(value))
                for value in logical_params["snapshot_ids"]
            ]
        logical_result_id = self._logical_snapshot_id(owned, result_snapshot_id)
        if logical_result_id == result_snapshot_id:
            logical_result_id = result_snapshot_id
        state.steps.append({
            "method": method,
            "params": logical_params,
            "result_snapshot_id": logical_result_id,
            "result_state_sha256": _snapshot_state_sha256(result),
        })
        state.current_snapshot_id = logical_result_id
        owned.snapshot_aliases[logical_result_id] = result_snapshot_id
        owned.input_by_snapshot_id[result_snapshot_id] = input_sha256
        self._write_recovery(state)

    @staticmethod
    def _translated_params(
        params: Mapping[str, Any], aliases: Mapping[str, str]
    ) -> dict[str, Any]:
        translated = json.loads(json.dumps(dict(params)))
        for key in ("snapshot_id", "base_snapshot_id"):
            if isinstance(translated.get(key), str):
                if translated[key] not in aliases:
                    raise DFMError(
                        "geometry_recovery_invalid",
                        "A recovery step references an unknown snapshot.",
                        {"snapshot_id": translated[key]},
                    )
                translated[key] = aliases[translated[key]]
        if isinstance(translated.get("snapshot_ids"), list):
            try:
                translated["snapshot_ids"] = [
                    aliases[str(value)] for value in translated["snapshot_ids"]
                ]
            except KeyError as exc:
                raise DFMError(
                    "geometry_recovery_invalid",
                    "A snapshot merge recovery step references an unknown branch.",
                ) from exc
        return translated

    @staticmethod
    def _validated_recovery_geometry_refs(
        params: dict[str, Any], opened: Mapping[str, Any], input_sha256: str
    ) -> dict[str, Any]:
        revisions = params.get("revisions")
        scopes = params.get("scopes")
        records = revisions if isinstance(revisions, list) else scopes
        if not isinstance(records, list):
            return params
        artifacts = opened.get("artifacts")
        topology_path = Path(
            str(artifacts.get("topology_map") if isinstance(artifacts, Mapping) else "")
        )
        try:
            topology = json.loads(topology_path.read_text(encoding="utf-8"))
            by_index = {
                int(item["geometry_ref"]["index"]): dict(item["geometry_ref"])
                for item in topology.get("faces", [])
                if isinstance(item, dict) and isinstance(item.get("geometry_ref"), dict)
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise DFMError(
                "geometry_recovery_invalid",
                "The reopened model topology cannot validate persisted revisions.",
            ) from exc
        for record in records:
            refs = record.get("geometry_refs")
            if not isinstance(refs, list):
                continue
            validated = []
            for old_ref in refs:
                if not isinstance(old_ref, Mapping):
                    raise DFMError(
                        "geometry_recovery_review_required",
                        "A persisted geometry revision contains an invalid reference.",
                    )
                new_ref = by_index.get(int(old_ref.get("index", 0)))
                if (
                    new_ref is None
                    or new_ref.get("kind") != "face"
                    or new_ref.get("input_sha256") != input_sha256
                    or (
                        old_ref.get("entity_id")
                        and old_ref.get("entity_id") != new_ref.get("entity_id")
                    )
                ):
                    raise DFMError(
                        "geometry_recovery_review_required",
                        "A persisted face revision cannot be verified on the reopened model.",
                        {"geometry_ref": dict(old_ref)},
                    )
                validated.append(new_ref)
            record["geometry_refs"] = validated
        return params

    def _recover_model(
        self,
        owned: ProjectGeometrySession,
        state: SnapshotRecoveryState,
    ) -> dict[str, Any]:
        persisted_identity = geometry_capability_identity(state.to_dict())
        live_identity = geometry_capability_identity(owned.session.capability)
        if persisted_identity != live_identity:
            raise DFMError(
                "geometry_recovery_dependency_changed",
                "The persisted geometry state belongs to a different Worker build.",
                {
                    "persisted_identity": persisted_identity,
                    "live_identity": live_identity,
                },
            )
        opened = owned.session.call(
            "snapshots.open",
            {
                "input_path": state.input_path,
                "input_sha256": state.input_sha256,
                "artifact_dir": state.artifact_dir,
                "lease_seconds": state.lease_seconds,
            },
        )
        live_opened_id = str(opened["snapshot_id"])
        aliases = {state.opened_snapshot_id: live_opened_id}
        owned.snapshot_ids.add(live_opened_id)
        owned.snapshot_lease_seconds[live_opened_id] = state.lease_seconds
        owned.snapshot_last_renewed[live_opened_id] = time.monotonic()
        owned.input_by_snapshot_id[live_opened_id] = state.input_sha256
        for step in state.steps:
            method = str(step.get("method") or "")
            if method not in _RECOVERABLE_SNAPSHOT_METHODS:
                raise DFMError(
                    "geometry_recovery_invalid",
                    "The geometry recovery log contains an unsupported method.",
                    {"method": method},
                )
            params = self._translated_params(step.get("params") or {}, aliases)
            params = self._validated_recovery_geometry_refs(
                params, opened, state.input_sha256
            )
            response = owned.session.call(method, params)
            if method in _ASYNC_SNAPSHOT_METHODS:
                response = owned.session.wait_job(
                    str(response["job_id"]),
                    timeout_seconds=self.job_timeout_seconds,
                    poll_interval_seconds=self.poll_interval_seconds,
                )
            logical_result_id = str(step.get("result_snapshot_id") or "")
            live_result_id = str(response.get("snapshot_id") or "")
            if not logical_result_id or not live_result_id:
                raise DFMError(
                    "geometry_recovery_invalid",
                    "A replayed recovery step did not produce a snapshot.",
                    {"method": method},
                )
            expected_state_sha256 = str(step.get("result_state_sha256") or "")
            actual_state_sha256 = _snapshot_state_sha256(response)
            if actual_state_sha256 != expected_state_sha256:
                raise DFMError(
                    "geometry_recovery_review_required",
                    "Replayed geometry state differs from the persisted reviewed state.",
                    {
                        "method": method,
                        "expected_state_sha256": expected_state_sha256,
                        "actual_state_sha256": actual_state_sha256,
                    },
                )
            aliases[logical_result_id] = live_result_id
            owned.snapshot_ids.add(live_result_id)
            owned.snapshot_lease_seconds[live_result_id] = state.lease_seconds
            owned.snapshot_last_renewed[live_result_id] = time.monotonic()
            owned.input_by_snapshot_id[live_result_id] = state.input_sha256
        current_live = aliases.get(state.current_snapshot_id)
        if not current_live:
            raise DFMError(
                "geometry_recovery_invalid",
                "The recovery log did not recreate its current snapshot.",
            )
        owned.snapshot_aliases.update(aliases)
        recovered = {
            **dict(opened),
            "snapshot_id": current_live,
            "recovered_from_snapshot_id": state.current_snapshot_id,
        }
        owned.snapshots_by_input_sha256[state.input_sha256] = recovered
        return recovered

    def _discard(self, project_id: str, session: GeometrySession) -> None:
        with self._lock:
            current = self._projects.get(project_id)
            if current is not None and current.session is session:
                self._projects.pop(project_id, None)

    def retain_project(self, project_id: str, reference_id: str) -> None:
        """Retain a project's Worker resources for one live chat/session."""

        if not project_id or not reference_id:
            return
        with self._lock:
            references = self._references.setdefault(project_id, set())
            references.add(reference_id)
            owned = self._projects.get(project_id)
            if owned is not None:
                owned.reference_ids.add(reference_id)
                owned.last_used = time.monotonic()

    def release_project(self, project_id: str, reference_id: str) -> None:
        """Release one chat/session reference without interrupting active jobs."""

        if not project_id or not reference_id:
            return
        with self._lock:
            references = self._references.get(project_id)
            if references is not None:
                references.discard(reference_id)
                if not references:
                    self._references.pop(project_id, None)
            owned = self._projects.get(project_id)
            if owned is not None:
                owned.reference_ids.discard(reference_id)
                owned.last_used = time.monotonic()

    @staticmethod
    def _is_evictable(owned: ProjectGeometrySession) -> bool:
        return not owned.reference_ids and not owned.active_job_ids

    def _evict_projects(self, project_ids: Sequence[str]) -> None:
        for project_id in project_ids:
            owned = self._projects.get(project_id)
            if owned is not None and self._is_evictable(owned):
                self._close_project_now(project_id, owned)

    def _evict_idle(self, *, exclude: str = "") -> None:
        now = time.monotonic()
        expired = [
            key
            for key, value in self._projects.items()
            if key != exclude
            and self._is_evictable(value)
            and now - value.last_used >= self.idle_timeout_seconds
        ]
        self._evict_projects(expired)

    def _evict_for_capacity(self, *, exclude: str = "") -> None:
        candidates = sorted(
            (
                (owned.last_used, project_id)
                for project_id, owned in self._projects.items()
                if project_id != exclude and self._is_evictable(owned)
            )
        )
        while len(self._projects) >= self.max_sessions and candidates:
            _, project_id = candidates.pop(0)
            self._evict_projects([project_id])

    def _evict_for_memory(
        self, *, required_bytes: int = 0, exclude: str = ""
    ) -> int:
        resident = sum(
            _resident_bytes(item.session.pid) for item in self._projects.values()
        )
        candidates = sorted(
            (
                (owned.last_used, project_id)
                for project_id, owned in self._projects.items()
                if project_id != exclude and self._is_evictable(owned)
            )
        )
        while resident + required_bytes > self.memory_budget_bytes and candidates:
            _, project_id = candidates.pop(0)
            owned = self._projects.get(project_id)
            if owned is None:
                continue
            resident -= _resident_bytes(owned.session.pid)
            self._evict_projects([project_id])
        return max(0, resident)

    def acquire(self, project_id: str) -> ProjectGeometrySession:
        with self._lock:
            self._evict_idle(exclude=project_id)
            current = self._projects.get(project_id)
            if current is not None and current.session.alive:
                current.last_used = time.monotonic()
                return current
            if current is not None:
                self._projects.pop(project_id, None)
                current.session.close()
            self._evict_for_capacity(exclude=project_id)
            if len(self._projects) >= self.max_sessions:
                raise DFMError(
                    "geometry_session_limit",
                    "No project-owned OCCT geometry session slot is available.",
                    {"max_sessions": self.max_sessions},
                )
            resident = self._evict_for_memory(exclude=project_id)
            if resident >= self.memory_budget_bytes:
                raise DFMError(
                    "geometry_memory_budget",
                    "The OCCT geometry session memory budget is exhausted.",
                    {
                        "resident_bytes": resident,
                        "memory_budget_bytes": self.memory_budget_bytes,
                    },
                )
            holder: ProjectGeometrySession | None = None

            def disconnected(_reason: str) -> None:
                if holder is not None:
                    self._discard(project_id, holder.session)

            session = GeometrySession(
                self.executable,
                request_timeout_seconds=self.request_timeout_seconds,
                on_disconnect=disconnected,
            )
            holder = ProjectGeometrySession(
                project_id,
                session,
                reference_ids=set(self._references.get(project_id, set())),
            )
            session_resident = _resident_bytes(session.pid)
            resident = self._evict_for_memory(
                required_bytes=session_resident, exclude=project_id
            )
            projected = resident + session_resident
            if projected > self.memory_budget_bytes:
                session.close()
                raise DFMError(
                    "geometry_memory_budget",
                    "Starting another OCCT geometry session would exceed the memory budget.",
                    {
                        "resident_bytes": projected,
                        "memory_budget_bytes": self.memory_budget_bytes,
                    },
                )
            self._projects[project_id] = holder
            return holder

    def call(
        self, project_id: str, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        owned = self.acquire(project_id)
        owned.last_used = time.monotonic()
        result = owned.session.call(method, params)
        snapshot_id = result.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            owned.snapshot_ids.add(snapshot_id)
            base_snapshot_id = str(
                params.get("snapshot_id") or params.get("base_snapshot_id") or ""
            )
            lease_seconds = owned.snapshot_lease_seconds.get(base_snapshot_id)
            if lease_seconds is not None:
                owned.snapshot_lease_seconds[snapshot_id] = lease_seconds
                owned.snapshot_last_renewed[snapshot_id] = time.monotonic()
        job_id = result.get("job_id")
        request_snapshot_id = str(params.get("snapshot_id") or "")
        if isinstance(job_id, str) and job_id and request_snapshot_id:
            owned.job_snapshot_ids[job_id] = request_snapshot_id
            owned.active_job_ids.add(job_id)
        if method in _RECOVERABLE_SNAPSHOT_METHODS:
            base_snapshot_id = str(
                params.get("snapshot_id") or params.get("base_snapshot_id") or ""
            )
            input_sha256 = owned.input_by_snapshot_id.get(base_snapshot_id, "")
            if method in _ASYNC_SNAPSHOT_METHODS:
                job_id = str(result.get("job_id") or "")
                if job_id and input_sha256:
                    owned.pending_snapshot_jobs[job_id] = (
                        method,
                        dict(params),
                        input_sha256,
                    )
            elif input_sha256:
                self._record_recovery_step(
                    project_id, owned, method, params, input_sha256, result
                )
        return result

    def update_snapshot(
        self,
        project_id: str,
        input_sha256: str,
        opened: Mapping[str, Any],
        derived: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Advance one project's current immutable snapshot after a v2 mutation."""

        owned = self.acquire(project_id)
        with self._lock:
            result = dict(derived)
            snapshot_id = str(result.get("snapshot_id") or "")
            opened_snapshot_id = str(opened.get("snapshot_id") or "")
            branch_base_id = str(result.get("base_snapshot_id") or "")
            if not snapshot_id or not branch_base_id:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "A snapshot-producing Worker method omitted its snapshot identity.",
                )
            cached = owned.snapshots_by_input_sha256.get(input_sha256)
            cached_snapshot_id = str((cached or {}).get("snapshot_id") or "")
            if cached_snapshot_id and cached_snapshot_id not in {
                branch_base_id,
                snapshot_id,
            }:
                result = self.call(
                    project_id,
                    "snapshots.merge",
                    {
                        "base_snapshot_id": branch_base_id,
                        "snapshot_ids": [cached_snapshot_id, snapshot_id],
                    },
                )
                snapshot_id = str(result["snapshot_id"])
            current = {
                **dict(opened),
                "snapshot_id": snapshot_id,
                "base_snapshot_id": result.get("base_snapshot_id"),
            }
            owned.snapshot_ids.add(snapshot_id)
            lease_seconds = owned.snapshot_lease_seconds.get(
                branch_base_id,
                owned.snapshot_lease_seconds.get(opened_snapshot_id),
            )
            if lease_seconds is not None:
                owned.snapshot_lease_seconds[snapshot_id] = lease_seconds
                owned.snapshot_last_renewed[snapshot_id] = time.monotonic()
            owned.input_by_snapshot_id[snapshot_id] = input_sha256
            owned.snapshots_by_input_sha256[input_sha256] = current
            owned.last_used = time.monotonic()
            return result

    def open_model(
        self,
        project_id: str,
        *,
        input_path: Path,
        input_sha256: str,
        artifact_dir: Path,
        lease_seconds: int,
    ) -> dict[str, Any]:
        owned = self.acquire(project_id)
        existing = owned.snapshots_by_input_sha256.get(input_sha256)
        if existing is not None:
            try:
                owned.session.call(
                    "snapshots.renew",
                    {
                        "snapshot_id": existing["snapshot_id"],
                        "lease_seconds": lease_seconds,
                    },
                )
                owned.snapshot_lease_seconds[str(existing["snapshot_id"])] = (
                    lease_seconds
                )
                owned.snapshot_last_renewed[str(existing["snapshot_id"])] = (
                    time.monotonic()
                )
                owned.last_used = time.monotonic()
                return dict(existing)
            except DFMError as exc:
                if exc.code not in {"snapshot_not_found", "geometry_session_lost"}:
                    raise
                owned.snapshots_by_input_sha256.pop(input_sha256, None)
        recovery = self._load_recovery(
            project_id,
            input_path=input_path,
            input_sha256=input_sha256,
            artifact_dir=artifact_dir,
            lease_seconds=lease_seconds,
        )
        if recovery is not None:
            try:
                recovered = self._recover_model(owned, recovery)
            except DFMError as exc:
                if exc.code != "geometry_recovery_dependency_changed":
                    raise
                self._recovery.get(project_id, {}).pop(input_sha256, None)
            else:
                owned.last_used = time.monotonic()
                return recovered
        opened = owned.session.call(
            "snapshots.open",
            {
                "input_path": str(input_path.resolve()),
                "input_sha256": input_sha256,
                "artifact_dir": str(artifact_dir.resolve()),
                "lease_seconds": lease_seconds,
            },
        )
        owned.snapshot_ids.add(opened["snapshot_id"])
        owned.snapshot_lease_seconds[str(opened["snapshot_id"])] = lease_seconds
        owned.snapshot_last_renewed[str(opened["snapshot_id"])] = time.monotonic()
        owned.input_by_snapshot_id[opened["snapshot_id"]] = input_sha256
        owned.snapshot_aliases[opened["snapshot_id"]] = opened["snapshot_id"]
        owned.snapshots_by_input_sha256[input_sha256] = dict(opened)
        state = SnapshotRecoveryState(
            input_path=str(input_path.resolve()),
            input_sha256=input_sha256,
            artifact_dir=str(artifact_dir.resolve()),
            lease_seconds=lease_seconds,
            opened_snapshot_id=str(opened["snapshot_id"]),
            current_snapshot_id=str(opened["snapshot_id"]),
            schema_bundle_version=str(
                owned.session.capability["schema_bundle_version"]
            ),
            engine_version=str(owned.session.capability["engine_version"]),
            dependency_fingerprint=str(
                owned.session.capability["dependency_fingerprint"]
            ),
        )
        self._recovery.setdefault(project_id, {})[input_sha256] = state
        self._write_recovery(state)
        owned.last_used = time.monotonic()
        return opened

    def wait_job(
        self,
        project_id: str,
        job_id: str,
        *,
        cancellation: Any | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        owned = self.acquire(project_id)
        request_snapshot_id = owned.job_snapshot_ids.get(job_id, "")
        lease_seconds = owned.snapshot_lease_seconds.get(request_snapshot_id, 900)
        owned.active_job_ids.add(job_id)

        def renew() -> None:
            if not request_snapshot_id:
                return
            owned.session.call(
                "snapshots.renew",
                {
                    "snapshot_id": request_snapshot_id,
                    "lease_seconds": lease_seconds,
                },
            )
            owned.snapshot_last_renewed[request_snapshot_id] = time.monotonic()

        close_after = False
        try:
            result = owned.session.wait_job(
                job_id,
                timeout_seconds=self.job_timeout_seconds,
                poll_interval_seconds=self.poll_interval_seconds,
                cancellation=cancellation,
                on_progress=on_progress,
                renew=renew,
                renew_interval_seconds=max(1.0, lease_seconds / 3),
            )
            owned.last_used = time.monotonic()
            snapshot_id = result.get("snapshot_id")
            if isinstance(snapshot_id, str) and snapshot_id:
                owned.snapshot_ids.add(snapshot_id)
                owned.snapshot_lease_seconds[snapshot_id] = lease_seconds
                owned.snapshot_last_renewed[snapshot_id] = time.monotonic()
            pending = owned.pending_snapshot_jobs.pop(job_id, None)
            if pending is not None:
                method, params, input_sha256 = pending
                self._record_recovery_step(
                    project_id, owned, method, params, input_sha256, result
                )
            return result
        finally:
            owned.active_job_ids.discard(job_id)
            owned.job_snapshot_ids.pop(job_id, None)
            owned.pending_snapshot_jobs.pop(job_id, None)
            close_after = owned.close_requested and not owned.active_job_ids
            if close_after:
                self._close_project_now(project_id, owned)

    def resolve_snapshot_id(self, project_id: str, snapshot_id: str) -> str:
        """Resolve a persisted logical snapshot to the current Worker handle."""

        owned = self.acquire(project_id)
        resolved = owned.snapshot_aliases.get(snapshot_id, snapshot_id)
        if resolved not in owned.snapshot_ids:
            raise DFMError(
                "geometry_snapshot_stale",
                "The persisted snapshot has not been restored in this Worker session.",
                {"snapshot_id": snapshot_id},
            )
        return resolved

    def _close_project_now(
        self,
        project_id: str,
        owned: ProjectGeometrySession,
    ) -> None:
        with self._lock:
            current = self._projects.get(project_id)
            if current is not owned:
                return
            self._projects.pop(project_id, None)
        for snapshot_id in sorted(owned.snapshot_ids):
            try:
                owned.session.call("snapshots.close", {"snapshot_id": snapshot_id})
            except DFMError:
                pass
        owned.session.close()

    def close_project(self, project_id: str) -> None:
        with self._lock:
            self._references.pop(project_id, None)
            owned = self._projects.get(project_id)
            if owned is not None:
                owned.reference_ids.clear()
            if owned is not None and owned.active_job_ids:
                owned.close_requested = True
                return
        if owned is None:
            return
        self._close_project_now(project_id, owned)

    def close(self) -> None:
        self._maintenance_stop.set()
        self._maintenance.join(timeout=10)
        with self._lock:
            project_ids = list(self._projects)
        for project_id in project_ids:
            self.close_project(project_id)
        with self._lock:
            self._references.clear()


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        cancellation: CancellationToken,
        on_event: Callable[[WorkerEvent], None],
        stdout_log_path: Path | None = None,
        stderr_log_path: Path | None = None,
    ) -> ProcessResult:
        if not argv or timeout_seconds <= 0:
            raise DFMError(
                "worker_request_invalid",
                "DFM worker argv must be non-empty and timeout must be positive.",
            )
        cwd = Path(cwd)
        cwd.mkdir(parents=True, exist_ok=True)
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                [str(item) for item in argv],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            raise DFMError(
                "worker_start_failed",
                "The DFM worker process could not be started.",
                {"executable": str(argv[0])},
            ) from exc

        stdout_queue: queue.Queue[str | None] = queue.Queue()
        stderr_parts: list[str] = []
        try:
            stdout_log = self._open_log(stdout_log_path)
            stderr_log = self._open_log(stderr_log_path)
        except OSError as exc:
            self._terminate_process_tree(process)
            raise DFMError(
                "worker_log_failed",
                "The DFM worker diagnostic logs could not be created.",
            ) from exc

        def read_stdout() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    if stdout_log is not None:
                        stdout_log.write(line)
                        stdout_log.flush()
                    stdout_queue.put(line)
            finally:
                stdout_queue.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_parts.append(line)
                if stderr_log is not None:
                    stderr_log.write(line)
                    stderr_log.flush()

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        stdout_parts: list[str] = []
        stdout_done = False
        deadline = time.monotonic() + timeout_seconds

        try:
            while not stdout_done or process.poll() is None:
                if cancellation.is_cancelled:
                    self._terminate_process_tree(process)
                    raise DFMError("run_cancelled", "The DFM run was cancelled.")
                if time.monotonic() >= deadline:
                    self._terminate_process_tree(process)
                    raise DFMError(
                        "worker_timeout",
                        "The DFM worker exceeded its execution timeout.",
                        {"timeout_seconds": timeout_seconds},
                    )
                try:
                    line = stdout_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    stdout_done = True
                    continue
                stdout_parts.append(line)
                event = parse_worker_event(line)
                if event is not None:
                    on_event(event)
            returncode = process.wait(timeout=1)
        except Exception:
            if process.poll() is None:
                self._terminate_process_tree(process)
            raise
        finally:
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            if stdout_log is not None:
                stdout_log.close()
            if stderr_log is not None:
                stderr_log.close()

        return ProcessResult(
            returncode=returncode,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
        )

    @staticmethod
    def _open_log(path: Path | None):
        if path is None:
            return None
        resolved = Path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved.open("w", encoding="utf-8", errors="replace")

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
