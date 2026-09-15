from __future__ import annotations

import json
import sys
import time


PREFIX = "__HERMES_DFM_EVENT__ "


def emit(payload):
    print(PREFIX + json.dumps(payload, separators=(",", ":")), flush=True)


def emit_raw(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)


mode = sys.argv[1]
if mode == "success":
    print("ordinary worker output", flush=True)
    print("fixture diagnostic", file=sys.stderr, flush=True)
    emit({"schema_version": 1, "type": "progress", "stage": "fixture", "percent": 50})
    emit({"schema_version": 1, "type": "completed", "path": "worker_result.json"})
elif mode == "hang":
    emit({"schema_version": 1, "type": "progress", "stage": "waiting", "percent": 1})
    while True:
        time.sleep(0.1)
elif mode == "raw_geometry":
    contract = "dfm.geometry.event/v1"
    emit_raw(
        {
            "schema_version": 1,
            "contract_version": contract,
            "type": "progress",
            "stage": "objective_load",
            "percent": 5,
        }
    )
    emit_raw(
        {
            "schema_version": 1,
            "contract_version": contract,
            "type": "completed",
            "path": "engine_result.json",
        }
    )
else:
    raise SystemExit(2)
