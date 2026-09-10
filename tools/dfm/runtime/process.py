"""Cross-platform isolated subprocess runner for DFM workers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Callable, Sequence

from ..analyzers.base import CancellationToken
from ..contracts import WorkerEvent
from ..errors import DFMError
from .events import parse_worker_event


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
