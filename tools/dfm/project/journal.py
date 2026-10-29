"""Small durable state deltas; no geometry objects or alternative state owner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..errors import DFMError


_KEYS = {
    "geometry_edits": ("edit_id",),
    "inputs": ("input_id",),
    "facts": ("fact_id",),
    "regions": ("region_id",),
    "clarifications": ("clarification_id",),
    "features": ("feature_id",),
    "observations": ("observation_id",),
    "fusion_links": ("fusion_link_id",),
    "discovery_snapshots": ("snapshot_id",),
    "forming_scopes": ("forming_scope_id", "snapshot_id"),
    "review_groups": ("review_group_id",),
    "plans": ("plan_id",),
    "runs": ("run_id",),
    "findings": ("finding_id",),
    "artifacts": ("artifact_id",),
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _identity(row: dict, keys: tuple[str, ...]) -> str:
    return _canonical([row[key] for key in keys]).decode("utf-8")


def _record_map(rows: list, keys: tuple[str, ...]) -> dict[str, dict] | None:
    if any(
        not isinstance(row, dict) or any(key not in row for key in keys) for row in rows
    ):
        return None
    result = {_identity(row, keys): row for row in rows}
    return result if len(result) == len(rows) else None


def difference(before: Any, after: Any, name: str = "") -> dict:
    if isinstance(before, dict) and isinstance(after, dict):
        return {
            "op": "map",
            "changes": {
                key: difference(before[key], value, key)
                if key in before
                else {"op": "set", "value": value}
                for key, value in after.items()
                if key not in before or before[key] != value
            },
            "remove": [key for key in before if key not in after],
        }
    keys = _KEYS.get(name)
    if keys and isinstance(before, list) and isinstance(after, list):
        old, new = _record_map(before, keys), _record_map(after, keys)
        if old is not None and new is not None:
            delta = {
                "op": "records",
                "keys": list(keys),
                "changes": {
                    key: difference(old[key], value)
                    if key in old
                    else {"op": "set", "value": value}
                    for key, value in new.items()
                    if key not in old or old[key] != value
                },
                "remove": [key for key in old if key not in new],
            }
            if list(old) != list(new):
                delta["order"] = list(new)
            return delta
    return {"op": "set", "value": after}


def apply(before: Any, delta: dict) -> Any:
    if delta["op"] == "set":
        return delta["value"]
    if delta["op"] == "map":
        result = dict(before)
        for key in delta["remove"]:
            del result[key]
        for key, change in delta["changes"].items():
            result[key] = apply(result.get(key), change)
        return result
    if delta["op"] == "records":
        records = _record_map(before, tuple(delta["keys"]))
        if records is None:
            raise ValueError("record identities are not unique")
        for key in delta["remove"]:
            del records[key]
        for key, change in delta["changes"].items():
            records[key] = apply(records.get(key), change)
        order = delta.get("order", list(records))
        if len(order) != len(records) or set(order) != set(records):
            raise ValueError("record ordering does not match its identities")
        return [records[key] for key in order]
    raise ValueError("unknown journal operation")


def encode(before: dict, after: dict) -> bytes:
    body = {
        "schema_version": 1,
        "project_id": after["project_id"],
        "base_revision": before["revision"],
        "revision": after["revision"],
        "changes": difference(before, after),
    }
    return (
        _canonical({**body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()})
        + b"\n"
    )


def replay(path: Path, checkpoint: dict) -> tuple[dict, int]:
    if not path.exists():
        return checkpoint, 0
    current = checkpoint
    committed_end = 0
    try:
        with path.open("rb") as stream:
            while raw := stream.readline():
                if not raw.endswith(b"\n"):
                    break  # Only an incomplete final append is discardable.
                event = json.loads(raw)
                checksum = event.pop("sha256")
                if (
                    event["schema_version"] != 1
                    or event["project_id"] != checkpoint["project_id"]
                    or hashlib.sha256(_canonical(event)).hexdigest() != checksum
                ):
                    raise ValueError("invalid event identity or checksum")
                if event["revision"] > checkpoint["revision"]:
                    if (
                        event["base_revision"] != current["revision"]
                        or event["revision"] != current["revision"] + 1
                    ):
                        raise ValueError("non-contiguous committed revision")
                    next_state = apply(current, event["changes"])
                    if (
                        next_state["revision"] != event["revision"]
                        or next_state["project_id"] != event["project_id"]
                    ):
                        raise ValueError("event does not produce its declared state")
                    current = next_state
                committed_end += len(raw)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise DFMError(
            "manifest_journal_invalid",
            "The project recovery log is invalid; no committed state was guessed.",
            {"path": str(path), "committed_bytes": committed_end},
        ) from exc
    return current, committed_end


def append(path: Path, event: bytes, committed_end: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() < committed_end:
            raise DFMError(
                "manifest_journal_invalid",
                "The committed project log was truncated externally.",
            )
        stream.truncate(committed_end)
        try:
            if stream.write(event) != len(event):
                raise OSError("short recovery-log write")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            # A failed commit must not be published from the process cache.
            try:
                stream.truncate(committed_end)
                stream.flush()
                os.fsync(stream.fileno())
            except OSError:
                pass
            raise
    return committed_end + len(event)
