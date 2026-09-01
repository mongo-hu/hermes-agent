import json

import pytest

from tools.dfm.contracts import GEOMETRY_EVENT_CONTRACT, WorkerEvent
from tools.dfm.errors import DFMError
from tools.dfm.runtime.events import EVENT_PREFIX, encode_worker_event, parse_worker_event


def test_worker_event_round_trip_uses_versioned_prefix():
    event = WorkerEvent(1, "progress", stage="load_geometry", percent=10)

    line = encode_worker_event(event)

    assert line.startswith(EVENT_PREFIX)
    assert parse_worker_event(line) == event


def test_non_event_output_is_ignored():
    assert parse_worker_event("ordinary diagnostic output") is None


def test_raw_geometry_jsonl_event_is_accepted_without_legacy_prefix():
    event = WorkerEvent(
        1,
        "progress",
        stage="objective_load",
        percent=5,
        contract_version=GEOMETRY_EVENT_CONTRACT,
    )

    assert parse_worker_event(json.dumps(event.to_dict())) == event
    assert parse_worker_event(json.dumps({"type": "ordinary-json-log"})) is None


def test_wall_thickness_face_progress_round_trips():
    event = WorkerEvent(
        1,
        "progress",
        stage="measure_wall_thickness_faces",
        percent=37,
        processed_faces=12,
        total_faces=32,
        elapsed_seconds=5.25,
        contract_version=GEOMETRY_EVENT_CONTRACT,
    )

    assert parse_worker_event(json.dumps(event.to_dict())) == event


@pytest.mark.parametrize(
    "details",
    [
        {"processed_faces": 2},
        {"processed_faces": 3, "total_faces": 2},
        {"processed_faces": 1, "total_faces": 2, "elapsed_seconds": -0.1},
    ],
)
def test_invalid_wall_thickness_face_progress_is_rejected(details):
    with pytest.raises(DFMError) as exc_info:
        WorkerEvent.from_dict(
            {
                "schema_version": 1,
                "type": "progress",
                "stage": "measure_wall_thickness_faces",
                "percent": 10,
                **details,
            }
        )

    assert exc_info.value.code == "worker_event_invalid"


@pytest.mark.parametrize(
    "line",
    [
        EVENT_PREFIX + "not-json",
        EVENT_PREFIX + json.dumps({"schema_version": 1, "type": "unknown"}),
    ],
)
def test_invalid_prefixed_event_is_rejected(line):
    with pytest.raises(DFMError) as exc_info:
        parse_worker_event(line)

    assert exc_info.value.code == "worker_event_invalid"
