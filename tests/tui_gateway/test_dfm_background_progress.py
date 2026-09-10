from tui_gateway import server


def test_dfm_background_progress_maps_to_desktop_tool_events(monkeypatch):
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
