"""Real Desktop review handoff and HTML preview, with an offline model provider.

All project, session, IPC, HTTP, WebSocket and WebView paths are production code.
The provider supplies only deterministic model decisions; user actions use the UI.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import Thread
import time

import pytest

from tests.tools.dfm.desktop_acceptance import electron_runtime, review_http_server
from tests.tools.dfm.test_desktop_view_menus import _same_camera, _settled_camera
from tests.tools.dfm.test_native_geometry_edits import model  # noqa: F401


pytestmark = pytest.mark.skipif(
    os.environ.get("DFM_DESKTOP_E2E") != "1",
    reason="explicit real Desktop acceptance is not enabled",
)


@contextmanager
def _review_agent(monkeypatch, service, project_id, *, on_provider_ready=None):
    from run_agent import AIAgent
    from tools import dfm_tool
    from tui_gateway import server as gateway

    requests = []
    agents = []
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: service)

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "messages" not in request:
                self.send_error(404)
                return
            requests.append(request)
            count = sum(row["role"] == "tool" for row in request["messages"])
            message = {"role": "assistant", "content": "后续分析计划已生成。"}
            finish = "stop"
            if count < 3:
                name, args = (
                    ("dfm_analysis", {"action": "discover", "project_id": project_id})
                    if count == 0 else
                    ("clarify", {"dfm_project_id": project_id, "dfm_discovery_review": True}) if count == 1 else
                    ("dfm_analysis", {"action": "plan", "project_id": project_id})
                )
                call = {"id": f"handoff-{len(requests)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}
                message = {"role": "assistant", "content": "", "tool_calls": [call]}
                finish = "tool_calls"
            self.send_response(200)
            if request.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = ({"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                         if finish == "tool_calls" else message)
                for content, reason in [(delta, None), ({}, finish)]:
                    chunk = {"id": "handoff", "choices": [{"index": 0, "delta": content, "finish_reason": reason}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                body = json.dumps({"id": "handoff", "object": "chat.completion", "created": 0,
                    "model": "handoff-test", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    if on_provider_ready:
        on_provider_ready(f"http://127.0.0.1:{provider.server_port}/v1")

    def make_agent(sid, key, **kwargs):
        agent = AIAgent(api_key="isolated-test", base_url=f"http://127.0.0.1:{provider.server_port}/v1",
            provider="openai-compat", model="handoff-test", max_iterations=8,
            enabled_toolsets=["dfm", "clarify"], quiet_mode=True, skip_context_files=True,
            skip_memory=True, save_trajectories=False, platform="desktop",
            session_id=kwargs.get("session_id") or key,
            session_db=kwargs.get("session_db") or gateway._get_db(), **gateway._agent_cbs(sid))
        agents.append(agent)
        return agent

    monkeypatch.setattr(gateway, "_make_agent", make_agent)
    try:
        yield requests
    finally:
        for agent in agents:
            agent.interrupt()
        gateway._shutdown_sessions()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)


def _prepare_profile(tmp_path, monkeypatch):
    import hermes_state
    from tui_gateway import server as gateway

    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: user_home))
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text(
        "model_catalog:\n  enabled: false\n", encoding="utf-8")
    # A test file shares one process but each test owns a different profile.
    # Re-open the real database for that profile instead of retaining the prior handle.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", Path(os.environ["HERMES_HOME"]) / "state.db")
    monkeypatch.setattr(gateway, "_db", None)


def _dismiss_provider(main):
    main.get_by_role("button", name=re.compile("稍后再选择提供方|I'll choose a provider later")).click(timeout=45000)


def _pending_review(gateway, project_id):
    return {request_id: payload for request_id, (event, payload) in gateway._pending_prompt_payloads.items()
            if event == "clarify.request" and payload.get("context", {}).get("project_id") == project_id}


def _review_handoff(model, tmp_path, monkeypatch, *, wait_seconds=0, reconnect=True):
    from playwright.sync_api import expect
    from tui_gateway import server as gateway
    from tools.dfm.contracts import EffectiveRule, GeometricRuleBinding
    from tools.dfm.processes.base import ProcessPlan

    service, project_id, wall, _ = model
    _prepare_profile(tmp_path, monkeypatch)
    review = service.analysis("review_context", project_id=project_id)
    service.analysis("restructure_forming_scopes", project_id=project_id,
        forming_scopes=[], retire_scope_ids=["SIDE_TEST"],
        expected_revision=review["revision"], expected_snapshot_id=review["snapshot_id"])
    review = service.analysis("review_context", project_id=project_id)
    binding = GeometricRuleBinding("fixture.handoff.wall", "G_WALL_THK_LOCAL", "fixture.handoff.rule",
                                   ">=", "identity", check_id="C_FIXTURE_WALL", operand_alias="actual")
    rules = ProcessPlan("injection", "fixture.handoff", "injection.geometry-core", "fixture",
        {binding.rule_id: EffectiveRule(0.1, "mm", "fixture:not_production")}, [], {"step"},
        rule_bindings=[binding], binding_selectors={binding.binding_id: {"actual": {"feature_kind": "main_wall"}}})
    monkeypatch.setattr("tools.dfm.service.compile_occt_injection_plan", lambda *_args, **_kwargs: rules)
    last = next(row for row in review["review_groups"] if row["kind"] == "ordinary_structure"
                and wall["feature_id"] in row["target_refs"])
    service.analysis("confirm_discovery", project_id=project_id,
        review_group_ids=[row["review_group_id"] for row in review["review_groups"]
                          if row["status"] == "pending_review" and row != last],
        expected_revision=review["revision"], expected_snapshot_id=review["snapshot_id"])
    with _review_agent(monkeypatch, service, project_id) as requests, \
            review_http_server(service, monkeypatch) as backend, \
            electron_runtime(tmp_path, backend=backend) as (main, _browser):
        _dismiss_provider(main)
        composer = main.locator('[contenteditable="true"]').first
        composer.fill("审核当前独立 DFM 验收模型，然后继续分析。")
        composer.press("Enter")
        deadline = time.monotonic() + 45
        while not _pending_review(gateway, project_id) and time.monotonic() < deadline:
            main.wait_for_timeout(100)
        pending = _pending_review(gateway, project_id)
        assert len(pending) == 1
        request_id = next(iter(pending))
        assert pending[request_id]["context"]["mode"] == "dfm_discovery_review"
        _assert_wait_survives(main, gateway, project_id, request_id, wait_seconds, tmp_path)
        (tmp_path / "review-before-reload.json").write_text(json.dumps({
            "url": main.url, "review": service.review_state(project_id),
            "sessions": {sid: {"session_key": row.get("session_key"), "key": row.get("key"),
                               "running": row.get("running")} for sid, row in gateway._sessions.items()},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # A reconnect must restore the same waiter, not create another model turn.
        reload_frames = []
        def capture_socket(socket):
            socket.on("framesent", lambda frame: reload_frames.append({"direction": "send", "data": frame}))
            socket.on("framereceived", lambda frame: reload_frames.append({"direction": "receive", "data": frame}))
        if reconnect:
            main.on("websocket", capture_socket)
            main.reload()
        panel = main.get_by_role("region", name="DFM 分组确认")
        try:
            expect(panel).to_be_visible(timeout=45000)
        finally:
            (tmp_path / "review-after-reload.json").write_text(json.dumps({
                "url": main.url, "frames": reload_frames,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        expect(panel.get_by_text("待确认 · 1 组；每组独立确认。", exact=True)).to_be_visible()
        panel.get_by_role("button", name="刷新", exact=True).click()
        assert set(_pending_review(gateway, project_id)) == {request_id}
        assert len(requests) == 2
        expect(main.get_by_text("请补充分析条件", exact=True)).to_have_count(0)
        # Expand the only pending structure and use its actual save operation.
        for summary in panel.locator("details > summary").all():
            if "主体壁" in summary.inner_text() and summary.locator("..").get_attribute("open") is None:
                summary.click()
        checkbox = panel.get_by_role("checkbox", name=f"选择 {wall['feature_id']}", exact=True)
        expect(checkbox).to_be_visible()
        checkbox.check()
        panel.get_by_role("button", name="确认选中组（1）", exact=True).click()
        expect(main.get_by_text("请补充分析条件", exact=True)).to_be_visible(timeout=45000)
        assert len(requests) == 2, "saving the last group must directly unblock the original clarify tool"
        batch = _pending_review(gateway, project_id)
        assert len(batch) == 1
        assert next(iter(batch.values()))["context"]["mode"] == "dfm_analysis_batch"
        assert request_id not in batch
        _assert_wait_survives(main, gateway, project_id, next(iter(batch)), wait_seconds, tmp_path)
        before = service._store(project_id).load()
        assert not any(row.status == "pending_review" for row in before.review_groups)
        main.screenshot(path=str(tmp_path / "right-review-opened-form.png"))
        main.get_by_role("textbox", name=re.compile("material grade")).fill("ABS")
        main.get_by_role("button", name=re.compile("提交全部并继续分析")).click()
        expect(main.get_by_text("后续分析计划已生成。", exact=True)).to_be_visible(timeout=30000)
        assert len(requests) == 4
        assert not _pending_review(gateway, project_id)
        latest = service._store(project_id).load()
        assert latest.features == before.features
        assert latest.review_groups == before.review_groups
        assert latest.runs == before.runs
        assert len(latest.plans) == len(before.plans) + 1
        assert latest.plans[-1].status == "ready"
        assert service.review_state(project_id)["open_clarification_count"] == 0


def _assert_wait_survives(main, gateway, project_id, request_id, seconds, tmp_path):
    if not seconds:
        return
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        main.wait_for_timeout(min(10000, (seconds - (time.monotonic() - start)) * 1000))
        pending = _pending_review(gateway, project_id)
        assert set(pending) == {request_id}, "the original DFM waiter must survive five minutes"
        (tmp_path / "long-wait-progress.json").write_text(json.dumps({
            "request_id": request_id, "elapsed_seconds": time.monotonic() - start,
            "mode": pending[request_id]["context"]["mode"], "still_pending": True,
        }, indent=2), encoding="utf-8")
    with (tmp_path / "long-wait-completed.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"request_id": request_id, "elapsed_seconds": time.monotonic() - start,
            "mode": pending[request_id]["context"]["mode"]}) + "\n")


def test_last_right_side_review_save_resumes_same_request_and_submits_form(model, tmp_path, monkeypatch):
    _review_handoff(model, tmp_path, monkeypatch)


def test_last_review_save_continues_without_reloading(model, tmp_path, monkeypatch):
    _review_handoff(model, tmp_path, monkeypatch, reconnect=False)


def _seed_report_history(gateway, project_id, viewer, report_path, *, session_id="desktop-report-history"):
    db = gateway._get_db()
    assert db.db_path.parent == Path(os.environ["HERMES_HOME"])
    db.create_session(session_id, source="desktop", cwd=str(report_path.parent))
    db.set_session_title(session_id, "DFM 独立报告浏览验收")
    db.append_message(session_id, "user", "查看已完成的独立 DFM 验收报告。")
    db.append_message(session_id, "assistant", "", tool_calls=[{"id": "completed-result", "type": "function",
        "function": {"name": "dfm_analysis", "arguments": json.dumps({"action": "result", "project_id": project_id,
                                                                      "run_id": "completed-preview-fixture"})}}])
    db.append_message(session_id, "tool", json.dumps({"status": "succeeded", "project_id": project_id,
        "run_id": "completed-preview-fixture", "viewer_manifest": str(viewer),
        "report": {"kind": "report_html", "path": str(report_path)}}, ensure_ascii=False),
        tool_name="dfm_analysis", tool_call_id="completed-result")
    db.append_message(session_id, "assistant", f'报告已生成。\n\nMEDIA:"{report_path}"')
    return session_id


def test_completed_history_report_opens_inside_workbench_and_preserves_model(model, tmp_path, monkeypatch):
    from playwright.sync_api import expect
    from tui_gateway import server as gateway

    service, project_id, _, _ = model
    _prepare_profile(tmp_path, monkeypatch)
    viewer = next(service.workspace.project_dir(project_id).glob("artifacts/geometry_sessions/*/dfm_discovery_viewer.json"))
    report = tmp_path / "离线报告 with spaces.html"
    report.write_text("<!doctype html><meta charset=utf-8><title>离线报告验收</title>"
        "<button id=interact onclick=\"this.textContent='交互已执行'\">验证交互</button>"
        "<script>document.documentElement.dataset.ready='yes';</script>"
        + "<!--" + "report payload " * 100000 + "-->", encoding="utf-8")
    history = _seed_report_history(gateway, project_id, viewer, report)
    before = service._store(project_id).load()
    with review_http_server(service, monkeypatch) as backend, \
            electron_runtime(tmp_path, backend=backend) as (main, _browser):
        _dismiss_provider(main)
        main.goto(main.url.split("#", 1)[0] + "#/" + history)
        canvas = main.locator('[aria-label="DFM 三维画布"]')
        expect(canvas).to_be_visible(timeout=45000)
        expect(main.get_by_role("button", name="查看报告", exact=True)).to_be_visible()
        expect(main.locator("webview")).to_have_count(0)
        camera = _settled_camera(main)
        canvas.evaluate("el => { el.dataset.acceptanceMounted = 'preserved'; }")
        # The persisted structured result authorizes the chat link's report identity.
        main.get_by_text("File: 离线报告 with spaces.html", exact=True).click()
        webview = main.locator("webview")
        expect(webview).to_have_count(1)
        expect(main.get_by_role("tab", name="报告", exact=True)).to_have_attribute("aria-selected", "true")
        main.wait_for_function("document.querySelector('webview')?.getURL?.().endsWith('.html')")
        ready = webview.evaluate("el => el.executeJavaScript('document.documentElement.dataset.ready')")
        assert ready == "yes"
        assert webview.get_attribute("webpreferences") == "contextIsolation=yes,nodeIntegration=no,sandbox=yes"
        assert webview.evaluate("el => el.executeJavaScript('typeof require')") == "undefined"
        assert webview.evaluate("el => el.executeJavaScript(\"document.getElementById('interact').click(); document.getElementById('interact').textContent\")") == "交互已执行"
        main.get_by_role("tab", name="模型", exact=True).click()
        expect(canvas).to_be_visible()
        assert canvas.get_attribute("data-acceptance-mounted") == "preserved"
        _same_camera(_settled_camera(main), camera)
        main.get_by_role("button", name="查看报告", exact=True).click()
        assert webview.evaluate("el => el.executeJavaScript(\"document.getElementById('interact').textContent\")") == "交互已执行"
        # Reopen the history: availability restores, but it does not steal the model.
        main.reload()
        expect(canvas).to_be_visible(timeout=45000)
        expect(main.get_by_role("button", name="查看报告", exact=True)).to_be_visible()
        expect(main.locator("webview")).to_have_count(0)
        after = service._store(project_id).load()
        assert after.runs == before.runs
        assert after.features == before.features
        assert after.revision == before.revision
    gateway._shutdown_sessions()
