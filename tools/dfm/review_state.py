"""Bounded Agent projection of one committed review revision.

The desktop receives full geometry through its existing review endpoint. Model
tools receive control state and a pageable group index, never checkpoint paths
or inline meshes/face lists. All identifiers and continuation arguments are exact.
"""

from collections import Counter, defaultdict
import json

from .errors import DFMError


REVIEW_PAGE_SIZE = 8
REVIEW_STATE_MAX_CHARS = 6500


def review_state(manifest, snapshot, groups, clarifications, *, cursor=None,
                 status="pending_review", group_ids=None):
    if status not in {"pending_review", "confirmed", "all"}:
        raise DFMError("review_query_invalid", "Use pending_review, confirmed, or all.")
    if group_ids is not None and (
        not isinstance(group_ids, list) or not group_ids or len(group_ids) > REVIEW_PAGE_SIZE
        or any(not isinstance(value, str) for value in group_ids)
    ):
        raise DFMError("review_query_invalid", f"Select between 1 and {REVIEW_PAGE_SIZE} review group IDs.")
    offset = 0
    if cursor is not None:
        try:
            revision, offset = (int(value) for value in cursor.split(":"))
        except (AttributeError, TypeError, ValueError):
            raise DFMError("review_query_invalid", "Invalid review cursor.") from None
        if revision != manifest.revision:
            raise DFMError("review_cursor_stale", "Review changed; restart the query without a cursor.",
                           {"revision": manifest.revision, "next_action": "review_context"})
        if offset < 0:
            raise DFMError("review_query_invalid", "Invalid review offset.")

    counts = defaultdict(Counter)
    for group in groups:
        counts[group.kind][group.status] += 1
    pending = [group for group in groups if group.status == "pending_review"]
    ordinary_pending = any(group.kind == "ordinary_structure" for group in pending)
    failure = manifest.capabilities.get("review_save_failure")
    saving = any(edit.get("status") == "pending" for edit in manifest.geometry_edits)
    run = next((run for run in reversed(manifest.runs) if run.status.value in {"queued", "running"}), None)
    next_call = None
    if failure or saving:
        next_action = "retry_review_save" if failure else "wait_review_save"
    elif ordinary_pending:
        next_action = "confirm_discovery"
        next_call = {"tool": "clarify", "arguments": {
            "dfm_project_id": manifest.project_id, "dfm_discovery_review": True}}
    elif clarifications:
        next_action = "clarify"
        next_call = {"tool": "clarify", "arguments": {
            "dfm_project_id": manifest.project_id,
            "dfm_fact_name": clarifications[0].clarification_id.removeprefix("clarification_")}}
    elif run:
        next_action = "report_context" if run.analyzer_key in {"occt_cpp", "step"} else "status"
        next_call = {"tool": "dfm_analysis", "arguments": {
            "action": next_action, "project_id": manifest.project_id, "run_id": run.run_id}}
        if next_action == "report_context":
            next_call["arguments"]["wait_seconds"] = 180
    else:
        next_action = "plan"
        next_call = {"tool": "dfm_analysis", "arguments": {
            "action": "plan", "project_id": manifest.project_id}}

    active = set(snapshot.feature_refs)
    features = [feature for feature in manifest.features if feature.feature_id in active]
    result = {
        "ok": True, "project_id": manifest.project_id, "phase": "discovery",
        "state_source": "committed_project", "revision": manifest.revision,
        "snapshot_id": snapshot.snapshot_id,
        "status": ("review_save_failed" if failure else "review_save_pending" if saving else
                   "discovery_review_required" if ordinary_pending else
                   "clarification_required" if clarifications else
                   "forming_scope_review_required" if pending else "discovery_confirmed"),
        "feature_count": len(features), "feature_counts": dict(Counter(feature.kind for feature in features)),
        "review_group_counts": {kind: dict(values) for kind, values in counts.items()},
        "pending_review_group_count": len(pending),
        "open_clarification_count": len(clarifications),
        "main_forming": [
            {"review_group_id": group.review_group_id, "status": group.status}
            for group in groups if group.kind == "product_forming"
        ],
        "requires_user_response": bool(pending or clarifications),
        "next_action": next_action, "next_call": next_call,
        "continuation_required": next_call is not None,
        "instruction": "Follow next_call. Read current state only through review_context; project_manifest.json is an internal checkpoint, not current state. Never reconfirm confirmed groups or infer missing engineering inputs.",
        "review_groups": [],
    }
    if failure:
        result["save_failure"] = {key: failure.get(key) for key in ("reason", "failure_category")}
    viewer_manifest = manifest.capabilities.get("discovery_review", {}).get("viewer_manifest")
    if viewer_manifest:
        result["viewer_manifest"] = viewer_manifest
    selected = [group for group in groups if group.review_group_id in group_ids] if group_ids else [
        group for group in groups if status == "all" or group.status == status]
    if group_ids and set(group_ids) != {group.review_group_id for group in selected}:
        raise DFMError("review_group_missing", "Requested review groups are no longer current.",
                       {"revision": manifest.revision, "next_action": "review_context"})
    selected.sort(key=lambda group: group.review_group_id)
    scopes = {scope["forming_scope_id"]: scope for scope in snapshot.forming_scopes}
    for group in selected[offset:offset + REVIEW_PAGE_SIZE]:
        row = {"review_group_id": group.review_group_id, "kind": group.kind, "status": group.status,
               "target_refs": group.target_refs[:REVIEW_PAGE_SIZE], "target_count": len(group.target_refs)}
        if group.kind in {"forming_scope", "product_forming"}:
            row["forming_scopes"] = [
                {key: scopes[ref].get(key) for key in ("forming_scope_id", "status", "draw_direction", "mechanism_kind")}
                for ref in group.target_refs[:REVIEW_PAGE_SIZE] if ref in scopes]
        result["review_groups"].append(row)
        if len(json.dumps(result, ensure_ascii=False)) > REVIEW_STATE_MAX_CHARS - 800:
            result["review_groups"].pop()
            break
    returned = len(result["review_groups"])
    if offset < len(selected) and not returned:
        raise DFMError("review_query_too_large", "This group cannot fit the review index; inspect it in the desktop review.")
    next_offset = offset + returned
    result["review_page"] = {"total": len(selected), "returned": returned, "status": status,
                             "next_cursor": f"{manifest.revision}:{next_offset}" if next_offset < len(selected) else None}
    return result
