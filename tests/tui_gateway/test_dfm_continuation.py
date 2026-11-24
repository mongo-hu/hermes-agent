"""DFM waits and recovery keep the originating conversation and user intent."""
import json
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def gateway(monkeypatch):
    from tui_gateway import server
    from tools import dfm_tool

    session = {
        "agent": SimpleNamespace(interrupt=lambda: None),
        "session_key": "stored-1", "history_lock": threading.Lock(), "source": "desktop",
        "history": [{"role": "assistant", "tool_calls": [{"function": {
            "name": "clarify", "arguments": json.dumps({"dfm_project_id": "project-1"}),
        }}]}], "running": False, "attached_images": ["unsent-image"],
    }
    state = {
        "project_id": "project-1", "snapshot_id": "snapshot-1", "revision": 8,
        "pending_review_group_count": 0, "next_action": "clarify",
        "next_call": {"tool": "clarify", "arguments": {"dfm_project_id": "project-1", "dfm_fact_name": "material"}},
    }
    monkeypatch.setattr(server, "_sessions", {"runtime-1": session})
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {})
    monkeypatch.setattr(server, "_answers", {})
    monkeypatch.setattr(dfm_tool, "_session_projects", {})
    monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: SimpleNamespace(review_state=lambda _project: state))
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    return server, session, state


def context(mode="dfm_discovery_review"):
    return {"mode": mode, "project_id": "project-1", "snapshot_id": "snapshot-1", "revision": 8}


def request(**overrides):
    return {
        "project_id": "project-1", "snapshot_id": "snapshot-1", "expected_revision": 8,
        "expected_session_key": "stored-1", "action_key": "review:8", **overrides,
    }


def prompt(server, **overrides):
    return server._methods["prompt.submit"]("rpc-1", {
        "session_id": "runtime-1", "text": "Continue", "dfm_continuation": request(**overrides),
    })


def reason(response):
    return response["error"]["data"]["reason_code"]


@pytest.mark.parametrize("mode,timeout", [("dfm_discovery_review", None), ("dfm_analysis_batch", None), ("ordinary", 300)])
def test_only_dfm_context_removes_human_input_timeout(gateway, monkeypatch, mode, timeout):
    server, _, _ = gateway
    seen = []
    monkeypatch.setattr(server, "_block", lambda *args, **kwargs: seen.append(kwargs["timeout"]) or "answer")
    server._agent_cbs("runtime-1")["clarify_callback"]("Question", [], context(mode))
    assert seen == [timeout]


def test_tui_retains_explicit_legacy_confirmation_and_timeout(gateway, monkeypatch):
    server, session, _ = gateway
    session["source"] = "tui"
    seen = []
    monkeypatch.setattr(server, "_block", lambda *args, **kwargs: seen.append(kwargs["timeout"]) or "answer")
    callback = server._agent_cbs("runtime-1")["clarify_callback"]
    assert not callback.supports_dfm_review_context()
    callback("Question", [], context("dfm_analysis_batch"))
    assert seen == [300]
    event = seed_wait(server)
    response = server._methods["clarify.respond"]("a", {"request_id": "question-1", "answer": "yes"})
    assert response["result"]["status"] == "ok"
    assert event.is_set()


@pytest.mark.parametrize("cancel", ["stop", "close"])
def test_unbounded_dfm_wait_is_released_by_stop_or_close(gateway, monkeypatch, cancel):
    server, session, _ = gateway
    shown = threading.Event()
    answer = []
    monkeypatch.setattr(server, "_emit", lambda *_args: shown.set())
    monkeypatch.setattr(server, "_teardown_session", lambda *_args, **_kwargs: None)
    worker = threading.Thread(target=lambda: answer.append(server._block(
        "clarify.request", "runtime-1", {"context": context()}, timeout=None)))
    worker.start()
    assert shown.wait(2)
    if cancel == "stop":
        server._methods["session.interrupt"]("stop", {"session_id": "runtime-1"})
    else:
        server._close_session_by_id("runtime-1")
    worker.join(2)
    assert not worker.is_alive()
    assert answer == [""]
    assert session["_dfm_wait_outcome"]["reason"] == "cancelled"
    assert not server._pending


def test_timeout_is_distinguished_from_an_explicit_stop(gateway):
    server, session, _ = gateway
    assert server._block("clarify.request", "runtime-1", {"context": context()}, timeout=0) == ""
    assert session["_dfm_wait_outcome"]["reason"] == "timeout"
    session["_turn_cancel_requested"] = True
    assert server._block("clarify.request", "runtime-1", {"context": context()}, timeout=None) == ""
    assert not server._pending


def test_late_dfm_wait_after_session_close_does_not_register(gateway, monkeypatch):
    server, _, _ = gateway
    server._sessions.clear()
    monkeypatch.setattr(server, "_emit", lambda *_args: pytest.fail("closed session must not emit a wait"))
    assert server._block("clarify.request", "runtime-1", {"context": context()}, timeout=None) == ""
    assert not server._pending


def seed_wait(server):
    event = threading.Event()
    server._pending["question-1"] = ("runtime-1", event)
    server._pending_prompt_payloads["question-1"] = ("clarify.request", {
        "request_id": "question-1", "question": "Review", "choices": ["Continue"], "context": context(),
    })
    return event


def test_resume_and_usage_replay_only_owned_unanswered_dfm_wait(gateway, monkeypatch):
    server, session, _ = gateway
    event = seed_wait(server)
    monkeypatch.setattr(server, "_fallback_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_get_usage", lambda *_args: {})
    monkeypatch.setattr("agent.account_usage.nous_credits_lines", lambda: [])
    payload = server._live_session_payload("runtime-1", session)
    usage = server._methods["session.usage"]("u", {"session_id": "runtime-1"})["result"]
    assert payload["pending_clarify"] == usage["pending_clarify"]
    assert payload["pending_clarify"]["session_id"] == "runtime-1"
    assert server._pending_dfm_clarify("other-session") is None
    event.set()
    assert server._pending_dfm_clarify("runtime-1") is None


def answer(server, **overrides):
    return server._methods["clarify.respond"]("answer", {
        "request_id": "question-1", "session_id": "runtime-1",
        "answer": "确认最终特征区域并继续", "dfm_context": request(), **overrides,
    })


def test_review_answer_requires_same_session_and_committed_snapshot(gateway):
    server, _, state = gateway
    event = seed_wait(server)
    assert reason(answer(server, session_id="other")) == "dfm_session_mismatch"
    assert reason(answer(server, dfm_context=request(expected_revision=7))) == "dfm_review_stale"
    state["pending_review_group_count"] = 1
    assert reason(answer(server)) == "dfm_review_incomplete"
    assert not event.is_set()
    state["pending_review_group_count"] = 0
    assert answer(server)["result"]["status"] == "ok"
    assert answer(server)["result"]["status"] == "duplicate"
    assert server._answers["question-1"] == "确认最终特征区域并继续"


def test_saved_edit_can_replace_snapshot_before_final_review_response(gateway):
    server, _, state = gateway
    event = seed_wait(server)
    state.update(snapshot_id="snapshot-edited", revision=9)
    result = answer(server, dfm_context=request(snapshot_id="snapshot-edited", expected_revision=9))
    assert result["result"]["status"] == "ok"
    assert event.is_set()
    assert server._pending_prompt_payloads["question-1"][1]["context"]["_accepted_state"] == {
        "snapshot_id": "snapshot-edited", "revision": 9}


def test_stopped_review_cannot_be_answered_by_a_late_success_callback(gateway):
    server, session, _ = gateway
    event = seed_wait(server)
    session["_dfm_stopped"] = True
    assert reason(answer(server)) == "dfm_continuation_stopped"
    assert not event.is_set()


@pytest.mark.parametrize("field,value,expected", [
    ("expected_session_key", "another-chat", "dfm_session_mismatch"),
    ("project_id", "another-project", "dfm_session_mismatch"),
    ("snapshot_id", "old-snapshot", "dfm_review_stale"),
    ("expected_revision", 7, "dfm_review_stale"),
])
def test_recovery_rejects_wrong_identity_without_a_turn(gateway, field, value, expected):
    server, session, _ = gateway
    assert reason(prompt(server, **{field: value})) == expected
    assert not session["running"]
    assert session["attached_images"] == ["unsent-image"]


def test_unknown_recovery_requires_manual_action_and_busy_never_interrupts(gateway):
    server, session, _ = gateway
    assert reason(prompt(server)) == "dfm_continuation_manual_required"
    session["running"] = True
    session["agent"].interrupt = lambda: pytest.fail("Recovery must not interrupt")
    assert reason(prompt(server, manual_resume=True)) == "dfm_session_busy"
    assert session.get("queued_prompt") is None
    session["running"] = False
    session["_dfm_stopped"] = True
    assert reason(prompt(server)) == "dfm_continuation_stopped"


def allow_dispatch(server, monkeypatch):
    calls = []
    for name in ("_ensure_session_db_row", "_persist_branch_seed", "_start_agent_build"):
        monkeypatch.setattr(server, name, lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *_args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *args: calls.append(args))

    class ImmediateThread:
        def __init__(self, target, **_kwargs):
            self.target = target
        def start(self):
            self.target()
        def is_alive(self):
            return False
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    return calls


@pytest.mark.parametrize("manual", [False, True])
def test_timeout_or_explicit_continue_dispatches_once_without_history_rewrite(gateway, monkeypatch, manual):
    server, session, state = gateway
    calls = allow_dispatch(server, monkeypatch)
    session["_dfm_wait_outcome"] = {**context(), "reason": "timeout"}
    session["_dfm_stopped"] = manual
    before = list(session["history"])
    assert prompt(server, manual_resume=manual)["result"]["status"] == "streaming"
    assert len(calls) == 1
    assert "project-1" in calls[0][-1]
    assert session["history"] == before
    assert session["attached_images"] == ["unsent-image"]
    assert prompt(server, manual_resume=manual)["result"]["status"] == "duplicate"
    assert prompt(server, action_key="other-key")["result"]["status"] == "duplicate"
    state["revision"] = 9  # A retry acknowledges the accepted action even after it advances state.
    assert prompt(server, manual_resume=manual)["result"]["status"] == "duplicate"
    assert len(calls) == 1


@pytest.mark.parametrize("status,result", [("running", "active_run"), ("reporting", "active_run"),
                                          ("succeeded", "completed"), ("cancelled", "completed")])
def test_existing_run_returns_its_identity_without_new_analysis(gateway, status, result):
    server, session, state = gateway
    state["workflow_run"] = {"run_id": "run-current", "status": status, "artifacts": []}
    state["next_action"] = "result" if result == "completed" else "report_context"
    session["running"] = result == "active_run"
    reply = prompt(server)["result"]
    assert reply["status"] == result
    assert reply["run_id"] == "run-current"
    assert reply["run_status"] == status
    assert session["running"] == (result == "active_run")


def test_idle_active_run_reenters_tracking_without_plan_or_start(gateway, monkeypatch):
    server, _, state = gateway
    calls = allow_dispatch(server, monkeypatch)
    state.update(workflow_run={"run_id": "run-current", "status": "reporting"}, next_action="report_context",
                 next_call={"tool": "dfm_analysis", "arguments": {
                     "project_id": "project-1", "run_id": "run-current", "action": "report_context"}})
    assert prompt(server, manual_resume=True)["result"]["status"] == "streaming"
    assert len(calls) == 1
    assert "report_context" in calls[0][-1]
    assert "禁止 plan/start" in calls[0][-1]


def test_analysis_batch_rejects_stale_revision_without_consuming_answers(gateway):
    server, _, _ = gateway
    event = seed_wait(server)
    server._pending_prompt_payloads["question-1"][1]["context"] = context("dfm_analysis_batch")
    assert reason(answer(server, dfm_context=request(expected_revision=6))) == "dfm_review_stale"
    assert not event.is_set()
    assert not server._answers


@pytest.mark.parametrize("continuation_text,keep_images", [("Continue", True), ("previous-turn", False), (None, False)])
def test_only_matching_continuation_turn_leaves_unsubmitted_images(gateway, monkeypatch, continuation_text, keep_images):
    server, session, _ = gateway
    if continuation_text:
        session["_dfm_continuation_action"] = {"action_key": "a", "text": continuation_text}
    class InertThread:
        def __init__(self, **_kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr(server.threading, "Thread", InertThread)
    server._run_prompt_submit("r", "runtime-1", session, "Continue")
    assert session["attached_images"] == (["unsent-image"] if keep_images else [])


def test_existing_pending_question_cannot_spawn_a_second_agent_turn(gateway):
    server, _, _ = gateway
    seed_wait(server)
    assert reason(prompt(server, manual_resume=True)) == "dfm_session_busy"
