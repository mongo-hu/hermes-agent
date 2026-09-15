import json
import hashlib
from pathlib import Path

from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.step import StepAnalyzer
from tools.dfm.contracts import (
    CapabilityStatus,
    EffectiveRule,
    InputRecord,
    LocalObjectiveWorkerRequest,
    ObjectiveArtifactManifest,
    ObjectiveResultManifest,
    PlanOperation,
    PlanRecord,
    WorkerEvent,
)
from tools.dfm.geometry.snapshot_hash import render_mesh_content_sha256
from tools.dfm.runtime.process import ProcessResult
from tools.dfm.workers.step_worker import WORKER_VERSION


class SuccessfulRunner:
    def __init__(self):
        self.request = None
        self.argv = None
        self.cwd = None
        self.timeout_seconds = None

    def run(
        self,
        argv,
        cwd,
        timeout_seconds,
        cancellation,
        on_event,
        stdout_log_path=None,
        stderr_log_path=None,
    ):
        self.argv = argv
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        request_path = Path(argv[-1])
        self.request = LocalObjectiveWorkerRequest.from_dict(
            json.loads(request_path.read_text(encoding="utf-8"))
        )
        output = Path(self.request.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        scene = output / "render_scene.json"
        scene.write_text(json.dumps({
            "schema_version": 2,
            "scene_id": "scene_geometry",
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "coordinate_system": "model",
            "unit": "mm",
            "topology_snapshot_ref": "topology_empty",
            "render_mesh_snapshot": {
                "render_mesh_snapshot_id": "mesh_test",
                "topology_snapshot_id": "topology_empty",
                "input_sha256": "a" * 64,
                "producer": "pythonocc",
                "producer_version": WORKER_VERSION,
                "tessellation": {},
                "triangle_count": 1,
                "render_mesh_sha256": render_mesh_content_sha256(
                    [
                        {
                            "primitive_id": "face-1",
                            "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                            "triangles": [[0, 1, 2]],
                        }
                    ]
                ),
            },
            "primitives": [{
                "primitive_id": "face-1",
                "render_mesh_snapshot_id": "mesh_test",
                "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                "triangles": [[0, 1, 2]],
            }],
        }), encoding="utf-8")
        topology = output / "topology_map.json"
        topology.write_text(json.dumps({
            "schema_version": 2,
            "map_id": "topology_geometry",
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "scene_ref": "scene_geometry",
            "topology_snapshot": {
                "topology_snapshot_id": "topology_empty",
                "input_sha256": "a" * 64,
                "backend": "pythonocc", "backend_version": WORKER_VERSION,
                "loader_id": "test-loader", "loader_version": "1",
                "indexer_id": "test-indexer", "indexer_version": "1",
                "entity_count": {"body": 1, "face": 0},
                "topology_content_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            },
            "render_mesh_snapshot_ref": "mesh_test",
            "faces": [],
        }), encoding="utf-8")
        field = output / "scalar_field_draft.json"
        field.write_text(json.dumps({
            "schema_version": 2,
            "field_id": "field_draft",
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "operation_id": "geometry.draft",
            "metric_id": "injection.geometry.draft",
            "quantity_id": "draft_angle_deg",
            "unit": "degree",
            "scene_ref": "scene_geometry",
            "topology_map_ref": "topology_geometry",
            "topology_snapshot_ref": "topology_empty",
            "render_mesh_snapshot_ref": "mesh_test",
            "interpolation": "linear_on_triangle",
            "calculation_context": {"pull_direction": [0, 0, 1]},
            "samples": [],
            "cells": [],
            "quality": {"backend": "pythonocc_demo", "certified": False},
        }), encoding="utf-8")
        measurements = output / "measurements.json"
        measurements.write_text(json.dumps({
            "schema_version": 1,
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "process": "injection",
            "scope_id": "injection.wall-draft",
            "producer_contract": "measurement_only",
            "measurements": [{
                "measurement_id": "measurement-draft",
                "operation_id": "geometry.draft",
                "calculator_id": "measure_draft",
                "metric_id": "injection.geometry.draft",
                "quantity_id": "draft_angle_deg",
                "value": 0.5,
                "unit": "degree",
                "status": "measured",
                "geometry_refs": [],
                "method": "pythonocc_triangulated_field",
                "algorithm_version": WORKER_VERSION,
                "input_sha256": "a" * 64,
                "quality": {"backend": "pythonocc_demo", "certified": False},
                "diagnostics": {},
                "region_refs": [],
                "field_refs": ["field_draft"],
            }],
        }), encoding="utf-8")
        def artifact(path, artifact_id, kind):
            content = path.read_bytes()
            return ObjectiveArtifactManifest(
                artifact_id,
                kind,
                path.name,
                "application/json",
                len(content),
                hashlib.sha256(content).hexdigest(),
            )

        result = ObjectiveResultManifest(
            schema_version=4,
            producer_version=WORKER_VERSION,
            run_id="run_1",
            input_sha256="a" * 64,
            process="injection",
            scope_id="injection.wall-draft",
            scope_version="2.0.0",
            result_path="worker_result.json",
            artifacts=[
                artifact(measurements, "measurements", "measurements"),
                artifact(scene, "scene_geometry", "render_scene"),
                artifact(topology, "topology_geometry", "topology_map"),
                artifact(field, "field_draft", "scalar_field"),
            ],
        )
        (output / result.result_path).write_text(
            json.dumps(result.to_dict()), encoding="utf-8"
        )
        on_event(WorkerEvent(1, "artifact", kind="measurements", path=measurements.name))
        on_event(WorkerEvent(1, "completed", path=result.result_path))
        return ProcessResult(0, "", "")


def test_step_capability_probe_is_stable_for_the_analyzer_lifetime(tmp_path):
    calls = []
    analyzer = StepAnalyzer(dependency_probe=lambda: calls.append(True) or True)
    context = AnalyzerContext("dfm_1", tmp_path, "step", [])

    capability = analyzer.capability(context)

    assert capability.status is CapabilityStatus.AVAILABLE
    assert analyzer.capability(context).status is CapabilityStatus.AVAILABLE
    assert capability.details["backend_id"] == "pythonocc_reference"
    assert capability.details["role"] == "reference_regression"
    assert capability.details["production_ready"] is False
    assert capability.details["production_target"] == "external_occt_cpp"
    assert len(calls) == 1


def test_step_analyzer_runs_persisted_plan_and_returns_contained_artifacts(tmp_path):
    input_path = tmp_path / "inputs" / "part.step"
    input_path.parent.mkdir()
    input_path.write_bytes(b"opaque-step")
    input_record = InputRecord(
        "input_1", "step", "part.step", "inputs/part.step", 11, "a" * 64, "now"
    )
    plan = PlanRecord(
        "plan_1",
        "step",
        ["step"],
        "ready",
        "now",
        process="injection",
        process_adapter_version="injection-wall-draft-v2",
        scope_id="injection.wall-draft",
        scope_version="2.0.0",
        input_ids=["input_1"],
        input_hashes={"input_1": "a" * 64},
        rules={
            "min_draft_deg": EffectiveRule(1.0, "degree", "scope")
        },
        operations=[
            PlanOperation(
                "geometry.draft",
                "measure_draft",
                metric_ids=["injection.geometry.draft"],
                required_quantities=["draft_angle_deg"],
                required_artifacts=["scalar_field", "render_scene", "topology_map"],
            )
        ],
    )
    runner = SuccessfulRunner()
    analyzer = StepAnalyzer(
        runner=runner,
        dependency_probe=lambda: True,
        python_executable="C:/dfm/python.exe",
        timeout_seconds=123,
    )
    context = AnalyzerContext("dfm_1", tmp_path, "step", [input_record], "run_1", plan)

    artifacts = analyzer.run(context, CancellationToken())

    assert runner.request.task.process == "injection"
    assert runner.request.task.scope_id == "injection.wall-draft"
    assert runner.argv[0] == "C:/dfm/python.exe"
    assert runner.cwd == Path(__file__).resolve().parents[3]
    assert runner.timeout_seconds == 123
    assert "rules" not in runner.request.task.to_dict()
    assert "max_evidence_findings" not in runner.request.task.to_dict()
    assert {artifact.kind for artifact in artifacts} == {
        "measurements",
        "render_scene",
        "topology_map",
        "scalar_field",
        "worker_result",
    }
    assert {artifact.artifact_id for artifact in artifacts} >= {
        "measurements",
        "scene_geometry",
        "topology_geometry",
        "field_draft",
    }
    assert all((tmp_path / artifact.relative_path).is_file() for artifact in artifacts)
