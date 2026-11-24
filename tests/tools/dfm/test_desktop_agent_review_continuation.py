"""Real Electron → gateway → AIAgent → tools → Worker confirmation round trip.

Only the external model provider is deterministic. It deliberately stops after
the review call; no UI events, tool results or IPC are mocked.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import Thread

import pytest

from tests.tools.dfm.desktop_acceptance import electron_runtime, review_http_server
from tests.tools.dfm.test_native_geometry_edits import model

pytestmark = pytest.mark.skipif(os.environ.get("DFM_DESKTOP_E2E") != "1", reason="real Desktop acceptance disabled")


@pytest.mark.parametrize("already_confirmed", [False, True])
def test_page_confirmation_opens_next_input_without_another_model_call(model, tmp_path, monkeypatch, already_confirmed):
    from playwright.sync_api import expect
    from run_agent import AIAgent
    from tui_gateway import server as gateway
    from tools import dfm_tool
    from tools.dfm.service import DFMService

    service, project_id, _, _ = model
    if already_confirmed:
        service.analysis("confirm_discovery", project_id=project_id)
        # Re-enter after a completed review with no warm Worker/binding cache,
        # as when the earlier confirmation prompt timed out or the app reopened.
        service.close()
        service = DFMService(config=service.config, workspace=service.workspace, reconcile_jobs=False)
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: user_home))
    requests = []
    agents = []
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: service)

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "messages" not in request:
                self.send_response(404)
                self.end_headers()
                return
            requests.append(request)
            tool_messages = [row for row in request["messages"] if row["role"] == "tool"]
            message = {"role": "assistant", "content": "分析参数已收到。"}
            finish_reason = "stop"
            if not tool_messages:
                name, arguments = "dfm_analysis", {
                    "action": "review_context" if already_confirmed else "discover", "project_id": project_id}
            elif len(tool_messages) == 1:
                name, arguments = "clarify", {"dfm_project_id": project_id, "dfm_discovery_review": True}
            if len(tool_messages) < 2:
                call = {"id": f"review-call-{len(requests)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}
                message = {"role": "assistant", "content": "", "tool_calls": [call]}
                finish_reason = "tool_calls"
            response = {"id": "review-response", "object": "chat.completion", "created": 0,
                "model": "review-test", "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = ({"role": "assistant", "tool_calls": [{"index": 0, **call}]}
                         if finish_reason == "tool_calls" else message)
                for delta, finish in [(delta, None), ({}, finish_reason)]:
                    chunk = {"id": "review-response", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=provider.serve_forever, daemon=True)
    thread.start()

    def make_agent(sid, key, **kwargs):
        agent = AIAgent(api_key="isolated-test", base_url=f"http://127.0.0.1:{provider.server_port}/v1",
            provider="openai-compat", model="review-test", max_iterations=8,
            enabled_toolsets=["dfm", "clarify"], quiet_mode=True,
            skip_context_files=True, skip_memory=True, save_trajectories=False,
            platform="desktop", session_id=kwargs.get("session_id") or key,
            session_db=kwargs.get("session_db") or gateway._get_db(), **gateway._agent_cbs(sid))
        agents.append(agent)
        return agent

    monkeypatch.setattr(gateway, "_make_agent", make_agent)
    try:
        with review_http_server(service, monkeypatch) as backend, electron_runtime(tmp_path, backend=backend) as (main, browser):
            # A fresh isolated profile has no real credentials. Dismiss the
            # production first-run picker through its normal control.
            main.get_by_role("button", name=re.compile("稍后再选择提供方|I'll choose a provider later")).click(timeout=45000)
            composer = main.locator('[contenteditable="true"]').first
            expect(composer).to_be_visible(timeout=30000)
            before = service._store(project_id).load()
            composer.fill("执行当前模型的确认续接回归测试。")
            composer.press("Enter")
            choice = main.get_by_role("button", name="确认最终特征区域并继续", exact=False)
            if not already_confirmed:
                expect(choice).to_be_visible(timeout=45000)
                before = service._store(project_id).load()
                choice.click()
                # Selecting the option and submitting use the actual production form.
                form = choice.locator("xpath=ancestor::form")
                form.locator('button[type="submit"]').click()
            expect(main.get_by_text("项补充信息，请在右侧 DFM 模型中统一填写并提交。", exact=False)).to_be_visible(timeout=45000)
            expect(main.get_by_text("请补充分析条件", exact=True)).to_be_visible()
            assert len(requests) == 2, "The review tool must open the form without another model decision"
            messages = [row for row in requests[-1]["messages"] if row["role"] == "tool"]
            state = service.review_state(project_id)
            assert state["main_forming"][0]["status"] == "confirmed"
            assert state["review_group_counts"]["ordinary_structure"].get("pending_review", 0) == 0
            assert state["next_call"]["tool"] == "clarify"
            expect(main.get_by_role("tab", name=f"补充信息 {state['open_clarification_count']}", exact=True)).to_be_visible()
            expect(main.get_by_text(f"{state['feature_count']} 个候选 · {state['feature_count']} 已确认", exact=True)).to_be_visible(timeout=20000)
            assert all("[Truncated:" not in row["content"] for row in messages)
            current = service._store(project_id).load()
            assert not current.capabilities.get("review_save_failure")
            assert current.facts == before.facts
            if already_confirmed:
                expect(choice).to_have_count(0)
                assert current.features == before.features
                assert current.capabilities.get("geometry_discovery") == before.capabilities.get("geometry_discovery")
                assert current.review_groups == before.review_groups
            main.screenshot(path=str(tmp_path / "review-continuation.png"))
            # This isolated plate's remaining input is its material grade.
            # Submit through the actual form, never through a synthetic RPC.
            main.get_by_role("textbox", name=re.compile("material grade")).fill("ABS")
            main.get_by_role("button", name=re.compile("提交全部并继续分析")).click()
            expect(main.get_by_text("分析参数已收到。", exact=True)).to_be_visible(timeout=30000)
            assert len(requests) == 3
            messages = [row for row in requests[-1]["messages"] if row["role"] == "tool"]
            confirmation = json.loads(messages[-1]["content"])
            assert "error" not in confirmation
            assert len(confirmation["dfm_facts"]) == state["open_clarification_count"]
            latest = service.review_state(project_id)
            assert confirmation["dfm_discovery_review"] == latest
            assert latest["open_clarification_count"] == 0
            assert confirmation["next_call"] == latest["next_call"]
            submitted = service._store(project_id).load()
            assert submitted.features == current.features
            assert submitted.capabilities.get("geometry_discovery") == current.capabilities.get("geometry_discovery")
            assert submitted.review_groups == current.review_groups
            assert submitted.runs == current.runs
            (tmp_path / "review-continuation.json").write_text(json.dumps({
                "tool_response_chars": [len(row["content"]) for row in messages],
                "state": state,
                "submitted_state": latest,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        for agent in agents:
            agent.interrupt()
        gateway._shutdown_sessions()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        service.close()
