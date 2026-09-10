"""Versioned JSON Lines protocol emitted by isolated DFM workers."""

from __future__ import annotations

import json

from ..contracts import GEOMETRY_EVENT_CONTRACT, WorkerEvent
from ..errors import DFMError


EVENT_PREFIX = "__HERMES_DFM_EVENT__ "


def encode_worker_event(event: WorkerEvent) -> str:
    return EVENT_PREFIX + json.dumps(
        event.to_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def parse_worker_event(line: str) -> WorkerEvent | None:
    stripped = line.strip()
    if stripped.startswith(EVENT_PREFIX):
        serialized = stripped[len(EVENT_PREFIX) :]
        claimed_event = True
    else:
        serialized = stripped
        claimed_event = False
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        if not claimed_event:
            return None
        raise DFMError(
            "worker_event_invalid",
            "DFM worker emitted invalid event JSON.",
        ) from exc
    if not isinstance(payload, dict):
        if not claimed_event:
            return None
        raise DFMError(
            "worker_event_invalid",
            "DFM worker event payload must be an object.",
        )
    if not claimed_event and payload.get("contract_version") != GEOMETRY_EVENT_CONTRACT:
        return None
    return WorkerEvent.from_dict(payload)
