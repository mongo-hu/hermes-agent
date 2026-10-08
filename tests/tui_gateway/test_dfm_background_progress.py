from queue import Queue
from types import SimpleNamespace
from contextlib import nullcontext
import json
import threading

import pytest


@pytest.fixture
def server():
    # The canonical Windows test runner clears profile paths until the
    # hermetic per-test environment fixture has initialized them.
    from tui_gateway import server as gateway_server

    return gateway_server


def test_dfm_background_progress_maps_to_desktop_tool_events(monkeypatch, server):
    emitted = []
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: True)
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload: emitted.append((event, sid, payload)),
    )

    server._on_tool_progress(
        "session-1",
        "background.tool.progress",
        "dfm_analysis",
        "DFM running: render_evidence (64%)",
        tool_id="tool-1",
        run_id="run-1",
        stage="render_evidence",
        percent=64,
        artifact_count=3,
        latest_artifact="runs/run-1/artifacts/DFM-001_front.png",
        latest_artifact_kind="evidence_image",
    )
    server._on_tool_progress(
        "session-1",
        "background.tool.complete",
        "dfm_analysis",
        "DFM succeeded: complete (100%)",
        tool_id="tool-1",
        run_id="run-1",
        status="succeeded",
        percent=100,
    )

    assert emitted[0][0] == "tool.progress"
    assert emitted[0][2]["tool_id"] == "tool-1"
    assert emitted[0][2]["percent"] == 64
    assert emitted[0][2]["latest_artifact_kind"] == "evidence_image"
    assert emitted[1][0] == "tool.complete"
    assert emitted[1][2]["status"] == "succeeded"


def test_dfm_reporting_queues_same_session_continuation_with_progress_hidden(monkeypatch, server):
    from tools.process_registry import process_registry

    notifications = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", notifications)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(
        server,
        "_sessions",
        {"session-1": {"session_key": "conversation-1", "_finalized": False}},
    )

    for _ in range(2):
        server._on_tool_progress(
            "session-1",
            "background.tool.progress",
            "dfm_analysis",
            "DFM reporting: report_editing (98%)",
            project_id="project-1",
            run_id="run-1",
            status="reporting",
        )

    assert notifications.qsize() == 1
    events = process_registry.drain_notifications(
        owns_event=lambda evt: server._session_owns_notification_event(
            "session-1", server._sessions["session-1"], evt
        )
    )
    assert len(events) == 1
    event, prompt = events[0]
    assert event["project_id"] == "project-1"
    assert event["run_id"] == "run-1"
    assert event["session_key"] == "conversation-1"
    assert "report_context" in prompt
    assert "render_html" in prompt
    assert server._notification_event_dedup_key(event) != server._notification_event_dedup_key(
        {**event, "run_id": "run-2"}
    )


def test_dfm_report_completion_queues_result_continuation(monkeypatch, server):
    from tools.process_registry import process_registry

    notifications = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", notifications)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(
        server,
        "_sessions",
        {"session-1": {"session_key": "conversation-1", "_finalized": False}},
    )

    server._on_tool_progress(
        "session-1",
        "background.tool.complete",
        "dfm_analysis",
        "DFM succeeded: complete (100%)",
        project_id="project-1",
        run_id="run-1",
        status="succeeded",
        report_html="C:\\reports\\run-1\\report.html",
    )

    events = process_registry.drain_notifications(
        owns_event=lambda evt: server._session_owns_notification_event(
            "session-1", server._sessions["session-1"], evt
        )
    )
    assert len(events) == 1
    event, prompt = events[0]
    assert event["type"] == "dfm_report_complete"
    assert event["report_html"] == "C:\\reports\\run-1\\report.html"
    assert "action=result" in prompt


def test_dfm_report_ready_notification_stays_in_its_owner_session(monkeypatch, server):
    from tools.process_registry import process_registry

    notifications = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", notifications)
    event = {
        "type": "dfm_report_ready",
        "project_id": "project-1",
        "run_id": "run-1",
        "origin_ui_session_id": "session-1",
        "session_key": "conversation-1",
    }
    notifications.put(event)
    foreign = {"session_key": "conversation-2", "_finalized": False}
    assert not process_registry.drain_notifications(
        owns_event=lambda evt: server._session_owns_notification_event(
            "session-2", foreign, evt
        )
    )
    assert notifications.qsize() == 1
    owner = {"session_key": "conversation-1", "_finalized": False}
    assert len(process_registry.drain_notifications(
        owns_event=lambda evt: server._session_owns_notification_event(
            "session-1", owner, evt
        )
    )) == 1


def test_dfm_report_ready_notification_is_skipped_after_report_succeeds(monkeypatch, server):
    from tools.dfm import service as service_module
    from tools.dfm.contracts import RunStatus

    run = SimpleNamespace(
        status=RunStatus.REPORTING,
        artifacts=[SimpleNamespace(kind="report_html_runtime")],
    )
    monkeypatch.setattr(
        service_module,
        "get_dfm_service",
        lambda: SimpleNamespace(jobs=SimpleNamespace(status=lambda *_ids: run)),
    )
    event = {
        "type": "dfm_report_ready",
        "project_id": "project-1",
        "run_id": "run-1",
    }
    assert server._dfm_report_notification_is_current(event)
    run.status = RunStatus.SUCCEEDED
    assert not server._dfm_report_notification_is_current(event)
    run.artifacts.append(SimpleNamespace(kind="report_html"))
    completed = {**event, "type": "dfm_report_complete"}
    assert server._dfm_report_notification_is_current(completed)


def test_completed_report_is_presented_from_artifacts_without_llm_turn(tmp_path, monkeypatch, server):
    from tools.dfm import service as service_module
    from tools.process_registry import process_registry

    report_path = tmp_path / "dfm_report.json"
    html_path = tmp_path / "report.html"
    report_path.write_text(json.dumps({"run_id": "run-1", "issues": [{"id": "issue-1"}]}), encoding="utf-8")
    html_path.write_text("<html></html>", encoding="utf-8")
    artifacts = [
        {"kind": "report_json", "path": str(report_path)},
        {"kind": "report_html", "path": str(html_path)},
    ]
    monkeypatch.setattr(
        service_module,
        "get_dfm_service",
        lambda: SimpleNamespace(analysis=lambda *_args, **_kwargs: {"run": {"artifacts": artifacts}}),
    )
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_session_db", lambda _session: nullcontext(None))
    monkeypatch.setattr(server, "_dfm_report_notification_is_current", lambda _evt: True)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_args: pytest.fail("completion must not call the LLM"))
    stop = threading.Event()
    emitted = []

    def emit(event, sid, payload=None):
        emitted.append((event, sid, payload))
        if event == "message.complete":
            stop.set()

    monkeypatch.setattr(server, "_emit", emit)
    queue = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", queue)
    event = {
        "type": "dfm_report_complete",
        "project_id": "project-1",
        "run_id": "run-1",
        "session_key": "conversation-1",
        "origin_ui_session_id": "session-1",
    }
    queue.put(event)
    session = {
        "session_key": "conversation-1",
        "history_lock": threading.RLock(),
        "history": [{"role": "assistant", "content": "报告生成中。"}],
        "running": False,
        "_finalized": False,
    }
    server._notification_poller_loop(stop, "session-1", session)

    assert [entry["role"] for entry in session["history"]] == ["assistant", "user", "assistant"]
    assert "1 项未通过" in session["history"][-1]["content"]
    assert str(html_path) in session["history"][-1]["content"]
    assert any(item[0] == "message.complete" for item in emitted)
    assert session["running"] is False
