"""Bounded report-editor data; full rendering evidence stays in the Runtime.

This is a projection at the model-tool boundary, not a replacement report.
Numerical outcomes, uncertainty, issue identities and drawing notes are lossless.
Per-face display evidence is represented by counts and assignment classes.
"""
from collections import Counter
from hashlib import sha256
import json

from ...errors import DFMError

REPORT_CONTEXT_MAX_CHARS = 6500
_RECORD_MAX_CHARS = 3200


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _editor_value(value):
    if isinstance(value, list):
        return [_editor_value(row) for row in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if isinstance(item, list) and (
            key in {"geometry_refs", "triangle_refs"} or key.endswith("_geometry_refs")
        ):
            result[key + "_count"] = len(item)
        elif key == "faces" and isinstance(item, list) and all(
            isinstance(row, dict) and "geometry_ref" in row for row in item
        ):
            # Keep each distinct status/reason/scope combination, including
            # overlapping pending scopes. Counts cover every source row.
            classes = Counter(_json({k: v for k, v in row.items() if k != "geometry_ref"}) for row in item)
            result["face_assignments"] = [
                {**json.loads(identity), "face_count": count}
                for identity, count in sorted(classes.items())
            ]
        else:
            result[key] = _editor_value(item)
    return result


def editor_runtime(runtime):
    result = {key: value for key, value in runtime.items() if key not in {"resources", "result_manifest"}}
    result["report"] = {key: value for key, value in runtime.get("report", {}).items() if key != "result_manifest"}
    return _editor_value(result)


def _records(path, value):
    record = {"path": path, "value": value}
    if len(_json(record)) <= _RECORD_MAX_CHARS:
        yield record
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _records([*path, key], item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _records([*path, index], item)
    elif isinstance(value, str):
        # Long drawing notes are split without dropping or rewriting any text.
        for offset in range(0, len(value), 400):
            chunk = {"path": path, "value": value[offset:offset + 400],
                     "text_offset": offset, "text_length": len(value)}
            if len(_json(chunk)) > _RECORD_MAX_CHARS:
                raise DFMError("report_context_invalid", "Report data has an excessively deep path.")
            yield chunk
    else:
        raise DFMError("report_context_invalid", "Report data cannot fit a context record.")


def report_editor_context(result, *, cursor=None):
    """Page one immutable report's editorial data, never a truncated JSON string."""
    if not result.get("ready") or "runtime" not in result:
        return result
    project_id, run_id = result["project_id"], result["run"]["run_id"]
    runtime = editor_runtime(result["runtime"])
    issue_ids = result["required_issue_ids"]
    identity = sha256(_json([project_id, run_id, result["runtime"], issue_ids]).encode()).hexdigest()
    records = list(_records(["runtime"], runtime))
    # IDs are separate records so even a very large issue set remains bounded.
    records.extend(_records(["required_issue_ids"], issue_ids))
    offset = 0
    if cursor is not None:
        try:
            saved_identity, offset_text = cursor.split(":")
            offset = int(offset_text)
        except (AttributeError, ValueError):
            raise DFMError("report_cursor_invalid", "Use the returned report_page.next_cursor.") from None
        if saved_identity != identity:
            raise DFMError("report_cursor_stale", "Report changed; restart report_context without a cursor.")
        if offset < 0 or offset >= len(records):
            raise DFMError("report_cursor_invalid", "Report cursor is outside this context.")
    page = {
        "ok": True, "project_id": project_id, "run_id": run_id, "ready": True,
        "complete": False, "context_contract": "dfm.report-editor/v1",
        "required_issue_count": len(issue_ids), "context_records": [],
        "instruction": (
            "Read every page by following next_call before render_html. Records are subtrees at exact JSON paths; "
            "combine all required_issue_ids and all Runtime facts across pages. For split text, concatenate by text_offset. "
            "Geometry counts and face assignment classes summarize display evidence only; numerical outcomes, coverage, "
            "uncertainty, issue IDs and drawing notes are preserved. Full rendering evidence remains server-side. "
            "Do not use terminal/read-file. Partial or indeterminate checks never imply a pass."
        ),
    }

    def continuation(end):
        more = end < len(records)
        next_cursor = f"{identity}:{end}" if more else None
        page.update({
            "context_complete": not more,
            "next_action": "report_context" if more else "render_html",
            "next_call": ({"tool": "dfm_analysis", "arguments": {
                "action": "report_context", "project_id": project_id, "run_id": run_id,
                "report_cursor": next_cursor}} if more else None),
            "report_page": {"offset": offset, "returned": end - offset, "total": len(records),
                            "next_cursor": next_cursor},
        })

    end = offset
    for record in records[offset:]:
        page["context_records"].append(record)
        continuation(end + 1)
        if len(json.dumps(page, ensure_ascii=False)) > REPORT_CONTEXT_MAX_CHARS:
            page["context_records"].pop()
            break
        end += 1
    continuation(end)
    if end == offset:
        raise DFMError("report_context_invalid", "Report context cannot fit a bounded response.")
    return page
