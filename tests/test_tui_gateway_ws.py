import asyncio
import threading
import time

from hermes_cli import mcp_startup
from tui_gateway import server
from tui_gateway import ws as ws_mod


def test_ws_startup_starts_background_mcp_discovery(monkeypatch):
    """The desktop app and dashboard chat reach the agent through this WS
    sidecar, not through tui_gateway.entry.main() (which spawns the discovery
    thread for the stdio TUI). handle_ws must start discovery itself, otherwise
    _make_agent's wait_for_mcp_discovery no-ops and the agent snapshots an
    MCP-less tool list. Regression test for #38945."""
    calls = []
    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **kw: calls.append(kw),
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    server._sessions.clear()
    try:
        asyncio.run(ws_mod.handle_ws(FakeWS()))
    finally:
        server._sessions.clear()

    assert calls == [{"logger": ws_mod._log, "thread_name": "tui-ws-mcp-discovery"}]


def _run_disconnect(monkeypatch, seed):
    """Drive handle_ws to its disconnect `finally`, seeding sessions against the
    live WSTransport the moment it exists. Returns nothing; inspect _sessions."""
    # Disable the grace-reap Timer: detached sessions normally schedule a
    # threading.Timer via _schedule_ws_orphan_reap, which would outlive the test
    # and fire _reap during interpreter teardown — touching _sessions/DB and
    # producing spurious post-run errors under the per-file CI runner. Grace=0
    # short-circuits the Timer (see _schedule_ws_orphan_reap) so the test leaves
    # no lingering thread.
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    # Mirror the real _finalize_session chokepoint: it is the single place that
    # closes the slash-worker (#38095). Stub it but keep that behavior so the
    # disconnect-reap path still exercises worker teardown.
    def _fake_finalize(s, end_reason="tui_close"):
        w = s.get("slash_worker")
        if w:
            w.close()

    monkeypatch.setattr(server, "_finalize_session", _fake_finalize)

    created = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod, "WSTransport",
        lambda ws, loop, **kw: created.append(real_transport(ws, loop, **kw)) or created[-1],
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            seed(created[0])  # transport now exists; attach it to sessions
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))


def test_ws_disconnect_reaps_flagged_session_and_closes_worker(monkeypatch):
    closed = []

    class FakeWorker:
        def close(self):
            closed.append(True)

    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                flagged={
                    "transport": t,
                    "close_on_disconnect": True,
                    "slash_worker": FakeWorker(),
                    "session_key": "k",
                }
            ),
        )
        assert "flagged" not in server._sessions
        assert closed == [True]
    finally:
        server._sessions.clear()


def test_ws_disconnect_preserves_and_repoints_reconnectable_session(monkeypatch):
    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                plain={"transport": t, "close_on_disconnect": False, "session_key": "k"}
            ),
        )
        assert server._sessions["plain"]["transport"] is server._detached_ws_transport
    finally:
        server._sessions.clear()


def test_ws_write_loop_stall_does_not_latch_transport(monkeypatch):
    """A write that times out because the event loop is stalled (GIL-heavy
    agent turn) must NOT latch the transport closed — the frame is already
    scheduled and flushes when the loop recovers. Latching here permanently
    silenced live watch windows after one slow write."""
    monkeypatch.setattr(ws_mod, "_WS_WRITE_TIMEOUT_S", 0.05)
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(line)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="stall-test")
        # Stall the loop well past the write timeout, then write from this
        # (non-loop) thread: the wait times out but the send stays in flight.
        loop.call_soon_threadsafe(time.sleep, 0.3)
        assert transport.write({"a": 1}) is True
        assert transport._closed is False

        # Once the loop breathes again, both the stalled frame and new writes
        # must reach the socket.
        assert transport.write({"b": 2}) is True
        deadline = time.time() + 2
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(sent) == 2
        assert transport._closed is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_step_upload_boundary_keeps_connection_usable(monkeypatch, tmp_path):
    """Exercise the real protocol and file.attach, without a model/LLM request."""
    import base64
    import hashlib
    import json
    import socket
    from pathlib import Path

    import uvicorn
    import websockets
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    from hermes_cli import web_server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "client-only" / "boundary.STP"
    assert not source.exists()
    monkeypatch.setattr(mcp_startup, "start_background_mcp_discovery", lambda **kwargs: None)
    monkeypatch.setitem(server._sessions, "ws-step-boundary", {
        "agent": object(), "session_key": "ws-step-boundary", "cwd": str(workspace),
    })
    application = Starlette(routes=[WebSocketRoute("/ws", ws_mod.handle_ws)])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    instance = uvicorn.Server(uvicorn.Config(
        application, host="127.0.0.1", port=port, lifespan="off", log_level="error",
        ws="websockets", ws_max_size=web_server._WS_MAX_MESSAGE_BYTES,
        ws_ping_interval=None, ws_ping_timeout=None,
    ))

    async def exercise():
        running = asyncio.create_task(instance.serve(sockets=[listener]))
        try:
            for _ in range(500):
                if instance.started:
                    break
                if running.done():
                    await running
                await asyncio.sleep(0.01)
            assert instance.started
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws", compression=None, ping_interval=None,
            ) as connection:
                ready = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
                assert ready["params"]["type"] == "gateway.ready"
                staged = workspace / ".hermes" / "desktop-attachments"
                for extra in (0, 1):
                    raw = b"x" * (server._STEP_ATTACHMENT_MAX_BYTES + extra)
                    digest = hashlib.sha256(raw).hexdigest()
                    encoded = base64.b64encode(raw).decode("ascii")
                    del raw
                    request = {
                        "id": f"boundary-{extra}", "method": "file.attach",
                        "params": {
                            "session_id": "ws-step-boundary", "path": str(source),
                            "name": "boundary.STP" if extra == 0 else "oversize.step",
                            "data_url": f"data:application/step;base64,{encoded}",
                        },
                    }
                    del encoded
                    await connection.send(json.dumps(request))
                    del request
                    response = json.loads(await asyncio.wait_for(connection.recv(), timeout=30))
                    assert response["id"] == f"boundary-{extra}"
                    if extra == 0:
                        assert response["result"]["uploaded"] is True
                        stored = Path(response["result"]["path"])
                        assert stored == staged / "boundary.STP"
                        assert stored.stat().st_size == server._STEP_ATTACHMENT_MAX_BYTES
                        with stored.open("rb") as stream:
                            assert hashlib.file_digest(stream, "sha256").hexdigest() == digest
                    else:
                        assert response["error"]["code"] == 5028
                        assert response["error"]["message"].startswith("oversize.step: ")
                        assert "upload limit" in response["error"]["message"]
                        assert list(staged.iterdir()) == [staged / "boundary.STP"]

                await connection.send(json.dumps({
                    "id": "after-rejection", "method": "file.attach",
                    "params": {
                        "session_id": "ws-step-boundary", "name": "after.txt",
                        "data_url": "YWxpdmU=",
                    },
                }))
                response = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
                assert response["id"] == "after-rejection"
                assert response["result"]["attached"] is True
                assert (staged / "after.txt").read_bytes() == b"alive"
        finally:
            instance.should_exit = True
            try:
                await asyncio.wait_for(running, timeout=10)
            finally:
                listener.close()

    asyncio.run(exercise())
