import io
import json
import threading
from queue import Queue
from types import SimpleNamespace

import pytest

from tools.dfm.runtime.process import GeometrySession, SESSION_CONTRACT


def test_reader_coalesces_progress_without_executing_application_callbacks():
    session = object.__new__(GeometrySession)
    session._job_event_lock = threading.Lock()
    session._job_events = {"job": None}
    session._pending_lock = threading.Lock()
    waiter = Queue()
    session._pending = {1: waiter}
    session._closed = threading.Event()
    session._closed.set()
    messages = [
        {
            "jsonrpc": "2.0",
            "method": "job.event",
            "params": {"job_id": "job", "progress": {"percent": i}},
        }
        for i in range(1000)
    ]
    messages.append({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"contract_version": SESSION_CONTRACT},
    })
    session._process = SimpleNamespace(
        stdout=io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    )
    session._read_responses()
    assert waiter.get_nowait()["id"] == 1
    assert len(session._job_events) == 1
    assert session._job_events["job"]["progress"]["percent"] == 999


def test_job_consumer_bounds_progress_and_always_delivers_terminal_state(monkeypatch):
    session = object.__new__(GeometrySession)
    session._job_event_lock = threading.Lock()
    session._job_events = {}
    tick = [0.0]
    count = [0]
    monkeypatch.setattr("tools.dfm.runtime.process.time.monotonic", lambda: tick[0])
    monkeypatch.setattr("tools.dfm.runtime.process.time.sleep", lambda duration: None)

    def call(method, params):
        count[0] += 1
        tick[0] += 0.01
        return {
            "status": "completed" if count[0] >= 1000 else "running",
            "progress": {"stage": "compute", "percent": count[0] // 10},
            "result": {"done": True},
        }

    session.call = call
    updates = []
    result = session.wait_job(
        "job", timeout_seconds=30, poll_interval_seconds=0, on_progress=updates.append
    )
    assert result == {"done": True}
    assert 2 <= len(updates) <= 22
    assert updates[-1]["status"] == "completed"
    assert session._job_events == {}


def test_application_callback_failure_is_reported_on_the_consumer_thread():
    session = object.__new__(GeometrySession)
    session._job_event_lock = threading.Lock()
    session._job_events = {}
    session.call = lambda *_args: {"status": "running", "progress": {"percent": 1}}
    thread = threading.get_ident()

    def progress(_status):
        assert threading.get_ident() == thread
        raise ValueError("state persistence failed")

    with pytest.raises(ValueError, match="state persistence failed"):
        session.wait_job(
            "job", timeout_seconds=5, poll_interval_seconds=0, on_progress=progress
        )
    assert session._job_events == {}
