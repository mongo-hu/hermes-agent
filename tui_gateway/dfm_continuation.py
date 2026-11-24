"""Desktop DFM continuation guards; no new model tool or workflow executor."""
import json


DFM_WAIT_MODES = frozenset({"dfm_discovery_review", "dfm_analysis_batch"})


class ContinuationError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def dfm_context(payload):
    context = payload.get("context")
    return context if isinstance(context, dict) and context.get("mode") in DFM_WAIT_MODES else None


def history_project(history):
    """Recover only an explicit tool binding, never a project named in prose."""
    for message in reversed(history):
        for call in reversed(message.get("tool_calls") or []):
            function = call.get("function") or {}
            if function.get("name") not in {"dfm_project", "dfm_analysis", "clarify"}:
                continue
            args = function.get("arguments") or {}
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict):
                project_id = args.get("project_id") or args.get("dfm_project_id")
                if isinstance(project_id, str) and project_id:
                    return project_id
    return None


def bound_project(session):
    from tools.dfm_tool import active_dfm_project_id

    return (active_dfm_project_id(session.get("session_key"))
            or session.get("_dfm_project_id")
            or history_project(session.get("history") or []))


def checked_state(service, context):
    project_id = context.get("project_id")
    snapshot_id = context.get("snapshot_id")
    revision = context.get("expected_revision")
    if (not isinstance(project_id, str) or not project_id or not isinstance(snapshot_id, str)
            or not snapshot_id or type(revision) is not int):
        raise ContinuationError("dfm_continuation_invalid", "DFM project, snapshot and revision are required.")
    state = service.review_state(project_id)
    if state.get("snapshot_id") != snapshot_id or state.get("revision") != revision:
        raise ContinuationError("dfm_review_stale", "DFM review changed; refresh before continuing.")
    if state.get("next_action") in {"wait_review_save", "retry_review_save"}:
        raise ContinuationError("dfm_review_save_pending", "Finish saving the DFM review before continuing.")
    return state


def prepare_continuation(session, request, service, *, has_pending=False):
    """Called under history_lock. Returns a no-op result or the exact next turn.

    Only a recorded timeout authorizes automatic recovery. After restart or a
    Stop, the right-side Continue action must explicitly set manual_resume.
    """
    if not isinstance(request, dict):
        raise ContinuationError("dfm_continuation_invalid", "Invalid DFM continuation.")
    action_key = request.get("action_key")
    if not isinstance(action_key, str) or not action_key or len(action_key) > 256:
        raise ContinuationError("dfm_continuation_invalid", "A bounded continuation action key is required.")
    project_id = request.get("project_id")
    if bound_project(session) != project_id:
        raise ContinuationError("dfm_session_mismatch", "This DFM project belongs to another conversation.")
    if not isinstance(request.get("snapshot_id"), str) or type(request.get("expected_revision")) is not int:
        raise ContinuationError("dfm_continuation_invalid", "DFM snapshot and revision are required.")
    manual = request.get("manual_resume") is True
    stopped = session.get("_dfm_stopped") or session.get("_turn_cancel_requested")
    if stopped and not manual:
        raise ContinuationError("dfm_continuation_stopped", "Analysis was stopped. Choose Continue to resume explicitly.")
    receipts = session.setdefault("_dfm_continuation_receipts", {})
    identity = (project_id, request["snapshot_id"], request["expected_revision"])
    receipt = receipts.get(action_key)
    if receipt is not None and receipt != identity:
        raise ContinuationError("dfm_continuation_key_conflict", "The continuation key belongs to another action.")
    if not stopped and (receipt is not None or identity in receipts.values()):
        return {"status": "duplicate", "action_key": action_key}, None
    state = checked_state(service, request)
    run = state.get("workflow_run") or {}
    active_run = run.get("status") in {"queued", "running", "reporting"}
    if run and (not active_run or session.get("running")):
        return {**run, "status": "active_run" if active_run else "completed", "run_status": run.get("status")}, None
    if session.get("running") or session.get("queued_prompt") or has_pending:
        raise ContinuationError("dfm_session_busy", "The conversation is busy; wait before continuing DFM.")
    outcome = session.get("_dfm_wait_outcome") or {}
    timeout_proven = (outcome.get("reason") == "timeout" and outcome.get("project_id") == project_id
                      and outcome.get("mode") == "dfm_discovery_review"
                      and type(outcome.get("revision")) is int
                      and outcome["revision"] <= request["expected_revision"])
    if not manual and not timeout_proven:
        raise ContinuationError("dfm_continuation_manual_required", "Choose Continue to restore this DFM workflow.")
    if state.get("pending_review_group_count"):
        raise ContinuationError("dfm_review_incomplete", "Finish the remaining DFM review before continuing.")
    next_call = state.get("next_call")
    if not next_call:
        raise ContinuationError("dfm_continuation_unavailable", "There is no DFM step to continue.")
    if active_run:
        arguments = next_call.get("arguments") or {}
        allowed = (next_call.get("tool") == "clarify" or (
            next_call.get("tool") == "dfm_analysis" and arguments.get("run_id") == run["run_id"]
            and arguments.get("action") in {"status", "report_context", "result"}))
        if not allowed:
            raise ContinuationError("dfm_continuation_unavailable", "The active Run must be followed without replanning.")
    # Preserve the existing conversation, tools, system prompt and role order.
    # This text becomes a normal next turn only after the caller claims idle.
    text = ("继续当前工程的 DFM 分析，使用已保存的审核与参数，不新建工程、不重新识别。"
            f"工程 {project_id}。先读取 review_context 核对最新状态，再执行其 next_call；"
            "若需补充信息直接打开现有表单，若已有 Run 则查看该 Run，不重复启动。"
            f"当前下一步：{json.dumps(next_call, ensure_ascii=False)}")
    if active_run:
        text += f" 现有 Run 为 {run['run_id']}；只跟踪此 Run，禁止 plan/start 或替换 Run。"
    receipts[action_key] = identity
    session["_dfm_stopped"] = False
    session["_dfm_continuation_action"] = {"action_key": action_key, "text": text}
    return None, text
