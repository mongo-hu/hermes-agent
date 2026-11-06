"""Real Electron → gateway → AIAgent → tools → Worker confirmation round trip.

Only the external model provider is deterministic. Its next call is selected
from the actual tool response; no UI events, tool results or IPC are mocked.
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


def test_page_confirmation_agent_receives_complete_state_and_opens_next_input(model, tmp_path, monkeypatch):
    from playwright.sync_api import expect
    from run_agent import AIAgent
    from tui_gateway import server as gateway
    from tools import dfm_tool
    from tools.dfm.service import DFMService

    service, project_id, _, _ = model
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: user_home))
    requests = []
    agents = []
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: service)

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal service
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "messages" not in request:
                self.send_response(404)
                self.end_headers()
                return
            requests.append(request)
            tool_messages = [row for row in request["messages"] if row["role"] == "tool"]
            if not tool_messages:
                name, arguments = "dfm_analysis", {"action": "discover", "project_id": project_id}
            elif len(tool_messages) == 1:
                name, arguments = "clarify", {"dfm_project_id": project_id, "dfm_discovery_review": True}
            elif len(tool_messages) == 2:
                # Deliberately re-read state after the user's successful click.
                # A new service has no Worker/binding cache, like app restart.
                service.close()
                service = DFMService(config=service.config, workspace=service.workspace, reconcile_jobs=False)
                monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: service)
                name, arguments = "dfm_analysis", {"action": "review_context", "project_id": project_id}
            else:
                state = json.loads(tool_messages[-1]["content"])
                call = state["next_call"]
                name, arguments = call["tool"], call["arguments"]
            call = {"id": f"review-call-{len(requests)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}
            response = {"id": "review-response", "object": "chat.completion", "created": 0,
                "model": "review-test", "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "", "tool_calls": [call]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            if request.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [({"role": "assistant", "tool_calls": [{"index": 0, **call}]}, None), ({}, "tool_calls")]:
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
            composer.fill("执行当前模型的确认续接回归测试。")
            composer.press("Enter")
            choice = main.get_by_role("button", name="确认最终特征区域并继续", exact=False)
            expect(choice).to_be_visible(timeout=45000)
            choice.click()
            # Selecting the option and submitting use the actual production form.
            form = choice.locator("xpath=ancestor::form")
            form.locator('button[type="submit"]').click()
            expect(main.get_by_text("项补充信息，请在右侧 DFM 模型中统一填写并提交。", exact=False)).to_be_visible(timeout=45000)
            assert len(requests) >= 4
            messages = [row for row in requests[-1]["messages"] if row["role"] == "tool"]
            confirmation = json.loads(messages[-2]["content"])
            state = json.loads(messages[-1]["content"])
            assert confirmation["dfm_discovery_review"]["revision"] == state["revision"]
            assert state["main_forming"][0]["status"] == "confirmed"
            assert state["review_group_counts"]["ordinary_structure"].get("pending_review", 0) == 0
            assert state["next_call"]["tool"] == "clarify"
            expect(main.get_by_text(f"{state['feature_count']} 个候选 · {state['feature_count']} 已确认", exact=True)).to_be_visible(timeout=20000)
            assert all("[Truncated:" not in row["content"] for row in messages)
            assert not service._store(project_id).load().capabilities.get("review_save_failure")
            main.screenshot(path=str(tmp_path / "review-continuation.png"))
            (tmp_path / "review-continuation.json").write_text(json.dumps({
                "tool_response_chars": [len(row["content"]) for row in messages],
                "state": state,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        for agent in agents:
            agent.interrupt()
        gateway._shutdown_sessions()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        service.close()
