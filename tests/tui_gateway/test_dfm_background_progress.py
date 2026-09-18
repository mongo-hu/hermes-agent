from queue import Queue
from types import SimpleNamespace

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
