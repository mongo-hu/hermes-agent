from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import time


PREFIX = "__HERMES_DFM_EVENT__ "


def emit(payload):
    print(PREFIX + json.dumps(payload, separators=(",", ":")), flush=True)


def emit_raw(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def serve_v2():
    """Serve deterministic protocol data; this fixture never certifies algorithms."""

    contract = "dfm.geometry/v2"
    dependency_fingerprint = "f" * 64
    snapshot_sequence = 0
    job_sequence = 0
    snapshots = {}
    jobs = {}

    def response(request_id, result):
        emit_raw({"jsonrpc": "2.0", "id": request_id, "result": result})

    def next_snapshot(base_snapshot_id=None):
        nonlocal snapshot_sequence
        snapshot_sequence += 1
        snapshot_id = f"snapshot_fixture_{snapshot_sequence:04d}"
        snapshots[snapshot_id] = deepcopy(
            snapshots.get(
                base_snapshot_id,
                {"features": [], "forming_scopes": [], "coverage": []},
            )
        )
        return snapshot_id

    def submit(method, result):
        nonlocal job_sequence
        job_sequence += 1
        job_id = f"job_fixture_{job_sequence:04d}"
        jobs[job_id] = {
            "contract_version": contract,
            "job_id": job_id,
            "method": method,
            "status": "completed",
            "progress": {"stage": method, "percent": 100},
            "result": result,
            "error": None,
        }
        return {"contract_version": contract, "job_id": job_id}

    def geometry_ref(input_sha256, topology_snapshot_id, index=1):
        return {
            "kind": "face",
            "index": index,
            "input_sha256": input_sha256,
            "topology_snapshot_id": topology_snapshot_id,
            "entity_id": f"face_{index:06d}",
        }

    def write_open_artifacts(params, topology_snapshot_id, mesh_snapshot_id):
        artifact_dir = Path(params["artifact_dir"]).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        input_sha256 = params["input_sha256"]
        face_ref = geometry_ref(input_sha256, topology_snapshot_id)
        paths = {
            "preflight": artifact_dir / "preflight.json",
            "topology_map": artifact_dir / "topology_map.json",
            "render_scene": artifact_dir / "render_scene.json",
        }
        payloads = {
            "preflight": {
                "schema_version": 1,
                "input_sha256": input_sha256,
                "fixture": True,
                "algorithm_certified": False,
            },
            "topology_map": {
                "schema_version": 2,
                "map_id": "topology_fixture",
                "run_id": "geometry_session_fixture",
                "input_sha256": input_sha256,
                "scene_ref": "scene_fixture",
                "topology_snapshot": {
                    "topology_snapshot_id": topology_snapshot_id,
                    "input_sha256": input_sha256,
                    "backend": "contract_fixture",
                    "backend_version": "1",
                    "loader_id": "contract-fixture-loader",
                    "loader_version": "1",
                    "indexer_id": "contract-fixture-indexer",
                    "indexer_version": "1",
                    "entity_count": {"body": 1, "face": 1},
                    "topology_content_sha256": "a" * 64,
                },
                "render_mesh_snapshot_ref": mesh_snapshot_id,
                "faces": [
                    {
                        "geometry_ref": face_ref,
                        "triangle_refs": [
                            {
                                "primitive_id": "body-1",
                                "triangle_id": 0,
                                "render_mesh_snapshot_id": mesh_snapshot_id,
                            }
                        ],
                    }
                ],
            },
            "render_scene": {
                "schema_version": 2,
                "scene_id": "scene_fixture",
                "run_id": "geometry_session_fixture",
                "input_sha256": input_sha256,
                "coordinate_system": "model",
                "unit": "mm",
                "topology_snapshot_ref": topology_snapshot_id,
                "render_mesh_snapshot": {
                    "render_mesh_snapshot_id": mesh_snapshot_id,
                    "topology_snapshot_id": topology_snapshot_id,
                    "input_sha256": input_sha256,
                    "producer": "contract_fixture",
                    "producer_version": "1",
                    "tessellation": {
                        "chordal_tolerance_mm": 0.05,
                        "angular_tolerance_deg": 5.0,
                    },
                    "triangle_count": 1,
                    "render_mesh_sha256": "b" * 64,
                },
                "primitives": [
                    {
                        "primitive_id": "body-1",
                        "render_mesh_snapshot_id": mesh_snapshot_id,
                        "vertices": [[0, 0, 0], [10, 0, 0], [0, 10, 0]],
                        "triangles": [[0, 1, 2]],
                    }
                ],
            },
        }
        for kind, path in paths.items():
            path.write_text(json.dumps(payloads[kind], indent=2), encoding="utf-8")
        return {key: str(value) for key, value in paths.items()}

    def feature(input_sha256, topology_snapshot_id):
        return {
            "feature_id": "MAIN_WALL_0001",
            "kind": "main_wall",
            "subtype": "fixture_wall",
            "confidence": 1.0,
            "input_sha256": input_sha256,
            "geometry_refs": [geometry_ref(input_sha256, topology_snapshot_id)],
            "parameters": {"fixture": True, "algorithm_certified": False},
            "relationships": [],
            "method": "contract_fixture",
            "diagnostics": {"fixture": True},
        }

    methods = {
        name: "available"
        for name in (
            "snapshots.open",
            "snapshots.renew",
            "snapshots.close",
            "features.discover",
            "features.revise",
            "forming_scopes.analyze",
            "forming_scopes.revise",
            "snapshots.merge",
            "geometrics.compute",
            "jobs.status",
            "jobs.cancel",
        )
    }
    geometrics = [
        {
            "geometric_id": geometric_id,
            "support_status": "supported",
            "feature_kind": "main_wall",
            "scope_modes": ["feature"],
            "direction_modes": (
                ["main", "local"] if "DRAFT" in geometric_id else ["none"]
            ),
            "requires_complete_inventory": False,
            "context_schema": (
                {
                    "type": "object",
                    "required": [
                        "forming_scope_id",
                        "draw_direction",
                        "coordinate_system",
                        "direction_source",
                    ],
                    "additionalProperties": False,
                }
                if "DRAFT" in geometric_id
                else {"type": "object", "additionalProperties": False}
            ),
        }
        for geometric_id in (
            "G_WALL_THK_MIN",
            "G_WALL_IN_DRAFT",
        )
    ]

    for line in sys.stdin:
        request = json.loads(line)
        request_id = request["id"]
        method = request["method"]
        params = request.get("params") or {}
        if method == "capabilities.get":
            response(
                request_id,
                {
                    "contract_version": contract,
                    "schema_bundle_version": "2.0.0",
                    "engine_version": "contract-fixture-1",
                    "dependency_fingerprint": dependency_fingerprint,
                    "backend": "analysis_situs+occt",
                    "transport": "stdio-jsonrpc",
                    "methods": methods,
                    "limits": {"max_concurrent_jobs": 1},
                    "occt_version": "fixture",
                    "analysis_situs_version": "fixture",
                    "analysis_situs_commit": "fixture",
                    "feature_kinds": ["main_wall", "rib", "screw_boss"],
                    "coverage_statuses": ["complete", "partial", "failed"],
                    "geometrics": geometrics,
                },
            )
        elif method == "snapshots.open":
            snapshot_id = next_snapshot()
            input_sha256 = params["input_sha256"]
            topology_snapshot_id = f"topology_{input_sha256[:16]}"
            mesh_snapshot_id = f"mesh_{input_sha256[:16]}"
            paths = write_open_artifacts(
                params, topology_snapshot_id, mesh_snapshot_id
            )
            snapshots[snapshot_id].update(
                {
                    "input_sha256": input_sha256,
                    "topology_snapshot_id": topology_snapshot_id,
                }
            )
            response(
                request_id,
                {
                    "contract_version": contract,
                    "snapshot_id": snapshot_id,
                    "base_snapshot_id": None,
                    "model_identity": {
                        "format": "step",
                        "input_sha256": input_sha256,
                        "unit": "mm",
                    },
                    "lease": {"expires_in_seconds": params.get("lease_seconds", 900)},
                    "artifacts": {
                        **paths,
                        "topology_snapshot_id": topology_snapshot_id,
                        "render_mesh_snapshot_id": mesh_snapshot_id,
                    },
                    "diagnostics": {
                        "model_import_count": 1,
                        "fixture": True,
                        "algorithm_certified": False,
                    },
                },
            )
        elif method == "snapshots.renew":
            response(
                request_id,
                {
                    "contract_version": contract,
                    "snapshot_id": params["snapshot_id"],
                    "valid": True,
                    "lease": {"expires_in_seconds": params.get("lease_seconds", 900)},
                    "diagnostics": {"fixture": True},
                },
            )
        elif method == "snapshots.close":
            response(
                request_id,
                {
                    "contract_version": contract,
                    "snapshot_id": params["snapshot_id"],
                    "closed": True,
                },
            )
        elif method == "features.discover":
            base_snapshot_id = params["snapshot_id"]
            snapshot_id = next_snapshot(base_snapshot_id)
            state = snapshots[snapshot_id]
            input_sha256 = state["input_sha256"]
            topology_snapshot_id = state["topology_snapshot_id"]
            requested = params.get("feature_kinds") or []
            context = params.get("context") or {}
            if "main_wall" in requested and not any(
                item["feature_id"] == "MAIN_WALL_0001" for item in state["features"]
            ):
                state["features"].append(feature(input_sha256, topology_snapshot_id))
            state["coverage"].extend(
                {
                    "feature_kind": kind,
                    "forming_scope_id": context.get("forming_scope_id"),
                    "status": "complete",
                    "covered_geometry_ref": geometry_ref(
                        input_sha256, topology_snapshot_id
                    ),
                    "unresolved_geometry_ref": None,
                    "diagnostics": {
                        "fixture": True,
                        "algorithm_certified": False,
                    },
                }
                for kind in requested
            )
            response(
                request_id,
                submit(
                    method,
                    {
                        "contract_version": contract,
                        "base_snapshot_id": base_snapshot_id,
                        "snapshot_id": snapshot_id,
                        "features": state["features"],
                        "coverage": state["coverage"],
                        "diagnostics": {"fixture": True},
                    },
                ),
            )
        elif method == "features.revise":
            base_snapshot_id = params["snapshot_id"]
            snapshot_id = next_snapshot(base_snapshot_id)
            state = snapshots[snapshot_id]
            revisions = {
                item["feature_id"]: item for item in params.get("revisions", [])
            }
            for item in state["features"]:
                revision = revisions.get(item["feature_id"])
                if revision:
                    item["geometry_refs"] = revision["geometry_refs"]
                    item["diagnostics"] = {
                        **item.get("diagnostics", {}),
                        "revision_source": revision.get("source"),
                    }
            response(
                request_id,
                {
                    "contract_version": contract,
                    "base_snapshot_id": base_snapshot_id,
                    "snapshot_id": snapshot_id,
                    "features": state["features"],
                    "coverage": state["coverage"],
                    "diagnostics": {"fixture": True},
                },
            )
        elif method == "forming_scopes.analyze":
            base_snapshot_id = params["snapshot_id"]
            snapshot_id = next_snapshot(base_snapshot_id)
            state = snapshots[snapshot_id]
            ref = geometry_ref(state["input_sha256"], state["topology_snapshot_id"])
            main_scope = {
                "forming_scope_id": params["forming_scope_id"],
                "mode": params["mode"],
                "draw_direction": [float(value) for value in params["draw_direction"]],
                "coordinate_system": params["coordinate_system"],
                "direction_source": params["direction_source"],
                "status": "candidate",
                "geometry_refs": [ref],
                "diagnostics": {"fixture": True, "algorithm_certified": False},
            }
            local_scope = {
                "forming_scope_id": "SIDE_01",
                "mode": "local",
                "draw_direction": None,
                "coordinate_system": "model",
                "direction_source": None,
                "status": "candidate",
                "geometry_refs": [ref],
                "diagnostics": {"fixture": True, "algorithm_certified": False},
            }
            state["forming_scopes"] = [main_scope, local_scope]
            state["coverage"].extend(
                {
                    "forming_scope_id": item["forming_scope_id"],
                    "status": "complete",
                    "covered_geometry_ref": ref,
                    "unresolved_geometry_ref": None,
                    "diagnostics": {"fixture": True},
                }
                for item in state["forming_scopes"]
            )
            response(
                request_id,
                submit(
                    method,
                    {
                        "contract_version": contract,
                        "base_snapshot_id": base_snapshot_id,
                        "snapshot_id": snapshot_id,
                        "features": state["features"],
                        "forming_scopes": state["forming_scopes"],
                        "coverage": state["coverage"],
                        "diagnostics": {"fixture": True},
                    },
                ),
            )
        elif method == "forming_scopes.revise":
            base_snapshot_id = params["snapshot_id"]
            snapshot_id = next_snapshot(base_snapshot_id)
            state = snapshots[snapshot_id]
            revisions = {
                item["forming_scope_id"]: item for item in params.get("scopes", [])
            }
            revised = []
            for item in state["forming_scopes"]:
                if item["forming_scope_id"] in revisions:
                    item = {
                        **revisions[item["forming_scope_id"]],
                        "status": "confirmed",
                        "diagnostics": {
                            "fixture": True,
                            "algorithm_certified": False,
                        },
                    }
                revised.append(item)
            state["forming_scopes"] = revised
            response(
                request_id,
                {
                    "contract_version": contract,
                    "base_snapshot_id": base_snapshot_id,
                    "snapshot_id": snapshot_id,
                    "features": state["features"],
                    "forming_scopes": state["forming_scopes"],
                    "coverage": state["coverage"],
                    "diagnostics": {"fixture": True},
                },
            )
        elif method == "snapshots.merge":
            base_snapshot_id = params["base_snapshot_id"]
            snapshot_id = next_snapshot(base_snapshot_id)
            state = snapshots[snapshot_id]
            for branch_id in params["snapshot_ids"]:
                branch = snapshots[branch_id]
                for key, identity in (
                    ("features", "feature_id"),
                    ("forming_scopes", "forming_scope_id"),
                ):
                    merged = {item[identity]: item for item in state[key]}
                    merged.update({item[identity]: item for item in branch[key]})
                    state[key] = list(merged.values())
                for coverage in branch["coverage"]:
                    if coverage not in state["coverage"]:
                        state["coverage"].append(coverage)
            response(
                request_id,
                {
                    "contract_version": contract,
                    "base_snapshot_id": base_snapshot_id,
                    "snapshot_id": snapshot_id,
                    "features": state["features"],
                    "forming_scopes": state["forming_scopes"],
                    "coverage": state["coverage"],
                    "diagnostics": {"fixture": True},
                },
            )
        elif method == "geometrics.compute":
            snapshot_id = params["snapshot_id"]
            state = snapshots[snapshot_id]
            ref = geometry_ref(state["input_sha256"], state["topology_snapshot_id"])
            values = {
                "G_WALL_THK_MIN": (1.0, "mm"),
                "G_WALL_IN_DRAFT": (1.5, "degree"),
            }
            results = []
            for item in params["items"]:
                for geometric_id in item["geometric_ids"]:
                    value, unit = values[geometric_id]
                    results.append(
                        {
                            "feature_id": item["feature_id"],
                            "geometric_id": geometric_id,
                            "status": "ok",
                            "value": value,
                            "unit": unit,
                            "context": item.get("context") or {},
                            "evidence": {
                                "geometry_refs": [ref],
                                "field_refs": [],
                            },
                            "quality": {
                                "fixture": True,
                                "certified": False,
                                "algorithm_certified": False,
                            },
                            "reason": None,
                            "provenance": {
                                "source_snapshot_id": snapshot_id,
                                "source_result_ref": (
                                    f"fixture:{item['feature_id']}:{geometric_id}"
                                ),
                                "dependency_fingerprint": dependency_fingerprint,
                            },
                        }
                    )
            response(
                request_id,
                submit(
                    method,
                    {
                        "contract_version": contract,
                        "snapshot_id": snapshot_id,
                        "status": "completed",
                        "results": results,
                        "artifacts": [],
                        "diagnostics": {
                            "fixture": True,
                            "algorithm_certified": False,
                        },
                    },
                ),
            )
        elif method == "jobs.status":
            response(request_id, jobs[params["job_id"]])
        elif method == "jobs.cancel":
            job = jobs[params["job_id"]]
            job["status"] = "cancelled"
            response(request_id, job)
        else:
            emit_raw(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "Method not found",
                        "data": {"code": "method_not_found"},
                    },
                }
            )


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
elif mode == "session_v2":
    serve_v2()
else:
    raise SystemExit(2)
