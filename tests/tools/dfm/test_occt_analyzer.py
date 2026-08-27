import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools.dfm.analyzers import occt as occt_module
from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.occt import (
    ENGINE_VERSION,
    OcctAnalyzer,
    discover_geometry_executable,
)
from tools.dfm.contracts import (
    GEOMETRY_EVENT_CONTRACT,
    GEOMETRY_RESULT_CONTRACT,
    OBJECTIVE_SCHEMA_VERSION,
    ArtifactRecord,
    CapabilityStatus,
    InputRecord,
    LocalObjectiveWorkerRequest,
    ObjectiveArtifactManifest,
    ObjectiveResultManifest,
    PlanOperation,
    PlanRecord,
    RegionRecord,
    ResolvedArgument,
    WorkerEvent,
)
from tools.dfm.errors import DFMError
from tools.dfm.geometry.snapshot_hash import render_mesh_content_sha256
from tools.dfm.runtime.process import ProcessResult


OPERATION_PAIRS = (
    ("geometry.preflight", "geometry_preflight"),
    ("topology.index", "index_topology"),
    ("topology.aag", "build_aag"),
    ("measure_draft", "measure_draft"),
    ("measure_wall_thickness", "measure_wall_thickness"),
    ("measure_undercut", "measure_undercut"),
    ("measure_sharp_corner", "measure_sharp_corner"),
    ("recognize_drilled_hole", "recognize_drilled_hole"),
    ("recognize_blend", "recognize_blend"),
    ("recognize_shaft", "recognize_shaft"),
    ("recognize_cavity", "recognize_cavity"),
    ("recognize_convex_hull", "recognize_convex_hull"),
    ("recognize_isolated", "recognize_isolated"),
    ("recognize_canonical_surface", "recognize_canonical_surface"),
    ("recognize_surface_probe", "recognize_surface_probe"),
    ("recognize_chamfer", "recognize_chamfer"),
    ("recognize_rib", "recognize_rib"),
)

CAPABILITIES = {
    "contract_version": "dfm.geometry.capabilities/v1",
    "engine_version": ENGINE_VERSION,
    "objective_schema_version": OBJECTIVE_SCHEMA_VERSION,
    "backend": "analysis_situs+occt",
    "analysis_situs_version": "v2025.2",
    "analysis_situs_commit": "aa5958932c8c85c068566ab685f2b99c0436b926",
    "occt_version": "7.9.3",
    "status": "available",
    "maturity": "experimental",
    "supported_processes": ["injection"],
    "supported_formats": ["step"],
    "supported_extensions": [".step", ".stp"],
    "output_artifact_kinds": [
        "preflight",
        "topology_map",
        "render_scene",
        "features",
        "measurements",
        "scalar_field",
    ],
    "operations": [
        {
            "operation_id": operation_id,
            "calculator_id": calculator_id,
            "maturity": "experimental",
            "algorithm_version": ENGINE_VERSION,
        }
        for operation_id, calculator_id in OPERATION_PAIRS
    ],
}


def _artifact(path: Path, artifact_id: str, kind: str) -> ObjectiveArtifactManifest:
    content = path.read_bytes()
    return ObjectiveArtifactManifest(
        artifact_id,
        kind,
        path.name,
        "application/json",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


class SuccessfulRunner:
    def __init__(self, *, healed=False):
        self.request = None
        self.argv = None
        self.healed = healed

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
        self.argv = list(argv)
        request_path = Path(argv[-1])
        self.request = LocalObjectiveWorkerRequest.from_dict(
            json.loads(request_path.read_text(encoding="utf-8"))
        )
        output = Path(self.request.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        identity = {
            "schema_version": 1,
            "run_id": self.request.task.run_id,
            "input_sha256": self.request.task.input_sha256,
        }
        preflight = output / "preflight.json"
        preflight.write_text(
            json.dumps({
                **identity,
                "contract_version": "dfm.geometry.artifact/preflight/v1",
                "engine_version": ENGINE_VERSION,
                "format": "step",
                "unit": "mm",
                "status": "passed",
                "valid_brep": True,
                "solid_count": 1,
                "face_count": 1,
                "edge_count": 0,
                "vertex_count": 0,
                "transferred_root_count": 1,
                "top_level_shape_type": "solid",
                "open_edge_count": 0,
                "non_manifold_edge_count": 0,
                "bbox": {},
                "surface_counts": {"plane": 1},
                "healed": self.healed,
                "geometry_refs": [],
                "quality": {
                    "backend": "occt",
                    "maturity": "experimental",
                    "certified": False,
                },
                "diagnostics": (
                    {
                        "step_shape_processing_disabled": False,
                        "shape_process_attempted": True,
                        "shape_process_operations": ["FixShape"],
                        "geometry_healing_applied": True,
                        "geometry_healing_succeeded": True,
                        "selected_transfer": "shape_processed",
                        "strict_validation": {
                            "analyzable": False,
                            "valid_brep": False,
                            "bbox_status": "whole",
                        },
                        "post_shape_process_validation": {
                            "analyzable": True,
                            "valid_brep": True,
                            "bbox_status": "finite",
                        },
                    }
                    if self.healed
                    else {}
                ),
            }),
            encoding="utf-8",
        )
        topology_snapshot_id = "topology_test"
        geometry_ref = {
            "kind": "face",
            "index": 1,
            "input_sha256": self.request.task.input_sha256,
            "topology_snapshot_id": topology_snapshot_id,
            "entity_id": "face-1",
        }
        primitive_content = [{
            "primitive_id": "face-1",
            "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
            "triangles": [[0, 1, 2]],
        }]
        mesh_sha256 = render_mesh_content_sha256(primitive_content)
        mesh_snapshot_id = f"mesh_{mesh_sha256[:16]}"
        primitives = [
            {**item, "render_mesh_snapshot_id": mesh_snapshot_id}
            for item in primitive_content
        ]
        topology_content = [
            {"entity_id": "face-1", "kind": "face", "index": 1}
        ]
        topology_sha256 = hashlib.sha256(
            json.dumps(
                topology_content,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        topology = output / "topology_map.json"
        topology.write_text(
            json.dumps({
                "schema_version": 2,
                "map_id": "topology_geometry",
                "run_id": self.request.task.run_id,
                "input_sha256": self.request.task.input_sha256,
                "scene_ref": "scene_geometry",
                "topology_snapshot": {
                    "topology_snapshot_id": topology_snapshot_id,
                    "input_sha256": self.request.task.input_sha256,
                    "backend": "analysis_situs+occt",
                    "backend_version": ENGINE_VERSION,
                    "loader_id": "occt-step-loader",
                    "loader_version": ENGINE_VERSION,
                    "indexer_id": "occt-topexp-face-indexer",
                    "indexer_version": "1",
                    "entity_count": {"body": 1, "face": 1},
                    "topology_content_sha256": topology_sha256,
                },
                "render_mesh_snapshot_ref": mesh_snapshot_id,
                "faces": [{
                    "geometry_ref": geometry_ref,
                    "triangle_refs": [{
                        "primitive_id": "face-1",
                        "triangle_id": 0,
                        "render_mesh_snapshot_id": mesh_snapshot_id,
                    }],
                }],
            }),
            encoding="utf-8",
        )
        render_scene = output / "render_scene.json"
        render_scene.write_text(
            json.dumps({
                "schema_version": 2,
                "scene_id": "scene_geometry",
                "run_id": self.request.task.run_id,
                "input_sha256": self.request.task.input_sha256,
                "coordinate_system": "model",
                "unit": "mm",
                "topology_snapshot_ref": topology_snapshot_id,
                "render_mesh_snapshot": {
                    "render_mesh_snapshot_id": mesh_snapshot_id,
                    "topology_snapshot_id": topology_snapshot_id,
                    "input_sha256": self.request.task.input_sha256,
                    "producer": "occt",
                    "producer_version": ENGINE_VERSION,
                    "tessellation": {"linear_deflection_mm": 0.1},
                    "triangle_count": 1,
                    "render_mesh_sha256": mesh_sha256,
                },
                "primitives": primitives,
            }),
            encoding="utf-8",
        )
        features = output / "features.json"
        features.write_text(
            json.dumps({
                **identity,
                "contract_version": "dfm.geometry.artifact/features/v1",
                "process": "injection",
                "scope_id": "injection.geometry-core",
                "scope_version": "4.0.0",
                "features": [
                    {
                        "feature_id": "feature-drilled-hole-1",
                        "kind": "drilled_hole",
                        "source_refs": ["input:" + self.request.task.input_sha256],
                        "confidence": 0.8,
                        "subtype": "through_hole",
                        "geometry_refs": [
                            geometry_ref
                        ],
                        "parameters": {"diameter_mm": 4.0},
                        "method": "analysis_situs_recognize_drill_holes",
                        "algorithm_version": ENGINE_VERSION,
                        "input_sha256": self.request.task.input_sha256,
                        "quality": {
                            "backend": "analysis_situs+occt",
                            "maturity": "experimental",
                            "certified": False,
                        },
                        "diagnostics": {},
                    }
                ],
            }),
            encoding="utf-8",
        )
        measurements = output / "measurements.json"
        draft_operation = next(
            item
            for item in self.request.task.operations
            if item.calculator_id == "measure_draft"
        )
        measurements.write_text(
            json.dumps({
                **identity,
                "contract_version": "dfm.geometry.artifact/measurements/v1",
                "process": "injection",
                "scope_id": "injection.geometry-core",
                "scope_version": "4.0.0",
                "producer_contract": "measurement_only",
                "measurements": [
                    {
                        "measurement_id": "measurement-draft-minimum",
                        "operation_id": draft_operation.operation_id,
                        "calculator_id": draft_operation.calculator_id,
                        "metric_id": draft_operation.metric_ids[0],
                        "quantity_id": draft_operation.required_quantities[0],
                        "value": 2.0,
                        "unit": "degree",
                        "status": "measured",
                        "geometry_refs": [
                            geometry_ref
                        ],
                        "method": "freecad_dfm_uv_grid_draft",
                        "algorithm_version": ENGINE_VERSION,
                        "input_sha256": self.request.task.input_sha256,
                        "quality": {
                            "backend": "occt",
                            "maturity": "experimental",
                            "certified": False,
                        },
                        "diagnostics": {},
                        "feature_refs": draft_operation.feature_refs,
                        "region_refs": draft_operation.region_refs,
                        "field_refs": ["scalar-field-measure-draft"],
                    }
                ],
            }),
            encoding="utf-8",
        )
        scalar_field = output / "scalar_field_1.json"
        scalar_field.write_text(
            json.dumps({
                "schema_version": 2,
                "field_id": "scalar-field-measure-draft",
                "run_id": self.request.task.run_id,
                "input_sha256": self.request.task.input_sha256,
                "operation_id": draft_operation.operation_id,
                "metric_id": draft_operation.metric_ids[0],
                "quantity_id": draft_operation.required_quantities[0],
                "unit": "degree",
                "scene_ref": "scene_geometry",
                "topology_map_ref": "topology_geometry",
                "topology_snapshot_ref": topology_snapshot_id,
                "render_mesh_snapshot_ref": mesh_snapshot_id,
                "interpolation": "constant_per_triangle",
                "calculation_context": {"pull_direction": [0, 0, 1]},
                "feature_refs": draft_operation.feature_refs,
                "region_refs": draft_operation.region_refs,
                "samples": [{
                    "sample_id": "draft-sample-1",
                    "point": [0.25, 0.25, 0],
                    "uv": [0.25, 0.25],
                    "surface_normal": [0, 0, 1],
                    "value": 2.0,
                    "geometry_ref": geometry_ref,
                    "mesh_vertex_ref": None,
                }],
                "cells": [{
                    "cell_id": "draft-cell-1",
                    "sample_ids": ["draft-sample-1"],
                    "geometry_ref": geometry_ref,
                    "triangle_ref": {
                        "primitive_id": "face-1",
                        "triangle_id": 0,
                        "render_mesh_snapshot_id": mesh_snapshot_id,
                    },
                }],
                "quality": {
                    "backend": "occt",
                    "maturity": "experimental",
                    "certified": False,
                },
            }),
            encoding="utf-8",
        )
        result = ObjectiveResultManifest(
            schema_version=OBJECTIVE_SCHEMA_VERSION,
            producer_version=ENGINE_VERSION,
            run_id=self.request.task.run_id,
            input_sha256=self.request.task.input_sha256,
            process="injection",
            scope_id="injection.geometry-core",
            scope_version="4.0.0",
            result_path="engine_result.json",
            artifacts=[
                _artifact(preflight, "preflight", "preflight"),
                _artifact(topology, "topology_geometry", "topology_map"),
                _artifact(render_scene, "scene_geometry", "render_scene"),
                _artifact(features, "features", "features"),
                _artifact(measurements, "measurements", "measurements"),
                _artifact(
                    scalar_field,
                    "scalar-field-measure-draft",
                    "scalar_field",
                ),
            ],
            contract_version=GEOMETRY_RESULT_CONTRACT,
        )
        (output / "engine_result.json").write_text(
            json.dumps(result.to_dict()), encoding="utf-8"
        )
        emitted = []
        for kind, path in (
            ("preflight", "preflight.json"),
            ("topology_map", "topology_map.json"),
            ("render_scene", "render_scene.json"),
            ("features", "features.json"),
            ("measurements", "measurements.json"),
            ("scalar_field", "scalar_field_1.json"),
            ("worker_result", "engine_result.json"),
        ):
            event = WorkerEvent(
                1,
                "artifact",
                kind=kind,
                path=path,
                contract_version=GEOMETRY_EVENT_CONTRACT,
            )
            emitted.append(event)
            on_event(event)
        completed = WorkerEvent(
            1,
            "completed",
            path="engine_result.json",
            contract_version=GEOMETRY_EVENT_CONTRACT,
        )
        emitted.append(completed)
        on_event(completed)
        stdout = "".join(
            json.dumps({
                key: value
                for key, value in event.to_dict().items()
                if value is not None
            })
            + "\n"
            for event in emitted
        )
        return ProcessResult(0, stdout, "")


class TamperingRunner(SuccessfulRunner):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def run(self, *args, **kwargs):
        on_event = args[4]
        result = super().run(*args, **kwargs)
        output = Path(self.request.output_dir)
        result_path = output / "engine_result.json"
        manifest = json.loads(result_path.read_text(encoding="utf-8"))
        if self.mode == "artifact_hash":
            (output / "measurements.json").write_text("{}", encoding="utf-8")
        elif self.mode == "input_hash":
            manifest["input_sha256"] = "b" * 64
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "path_escape":
            manifest["artifacts"][0]["filename"] = "../escape.json"
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "topology_ref":
            measurement_path = output / "measurements.json"
            measurements = json.loads(measurement_path.read_text(encoding="utf-8"))
            measurements["measurements"][0]["geometry_refs"][0]["index"] = 999
            measurement_path.write_text(json.dumps(measurements), encoding="utf-8")
            content = measurement_path.read_bytes()
            artifact = next(
                item for item in manifest["artifacts"] if item["kind"] == "measurements"
            )
            artifact["size_bytes"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "topology_hash":
            topology_path = output / "topology_map.json"
            topology = json.loads(topology_path.read_text(encoding="utf-8"))
            topology["topology_snapshot"]["topology_content_sha256"] = "0" * 64
            topology_path.write_text(json.dumps(topology), encoding="utf-8")
            content = topology_path.read_bytes()
            artifact = next(
                item for item in manifest["artifacts"] if item["kind"] == "topology_map"
            )
            artifact["size_bytes"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "mesh_hash":
            scene_path = output / "render_scene.json"
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            scene["render_mesh_snapshot"]["render_mesh_sha256"] = "0" * 64
            scene_path.write_text(json.dumps(scene), encoding="utf-8")
            content = scene_path.read_bytes()
            artifact = next(
                item
                for item in manifest["artifacts"]
                if item["kind"] == "render_scene"
            )
            artifact["size_bytes"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "algorithm_version":
            features_path = output / "features.json"
            features = json.loads(features_path.read_text(encoding="utf-8"))
            features["features"][0]["algorithm_version"] = "tampered"
            features_path.write_text(json.dumps(features), encoding="utf-8")
            content = features_path.read_bytes()
            artifact = next(
                item for item in manifest["artifacts"] if item["kind"] == "features"
            )
            artifact["size_bytes"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "healing_audit":
            preflight_path = output / "preflight.json"
            preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
            preflight["healed"] = True
            preflight_path.write_text(json.dumps(preflight), encoding="utf-8")
            content = preflight_path.read_bytes()
            artifact = next(
                item for item in manifest["artifacts"] if item["kind"] == "preflight"
            )
            artifact["size_bytes"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "duplicate_event":
            on_event(
                WorkerEvent(
                    1,
                    "artifact",
                    kind="features",
                    path="features.json",
                    contract_version=GEOMETRY_EVENT_CONTRACT,
                )
            )
        elif self.mode == "duplicate_kind":
            source = output / "measurements.json"
            duplicate = output / "measurements-copy.json"
            duplicate.write_bytes(source.read_bytes())
            artifact = dict(
                next(
                    item
                    for item in manifest["artifacts"]
                    if item["kind"] == "measurements"
                )
            )
            artifact["artifact_id"] = "measurements-copy"
            artifact["filename"] = duplicate.name
            manifest["artifacts"].append(artifact)
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "manifest_result_path":
            manifest["result_path"] = "other-result.json"
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif self.mode == "stdout_pollution":
            return ProcessResult(
                result.returncode,
                "OCCT diagnostic on stdout\n" + result.stdout,
                result.stderr,
            )
        elif self.mode == "error_on_success":
            error = WorkerEvent(
                1,
                "error",
                code="objective_calculation_failed",
                message="contradictory terminal event",
                contract_version=GEOMETRY_EVENT_CONTRACT,
            )
            on_event(error)
            serialized = json.dumps({
                key: value
                for key, value in error.to_dict().items()
                if value is not None
            })
            return ProcessResult(
                result.returncode, result.stdout + serialized + "\n", result.stderr
            )
        else:
            raise AssertionError(self.mode)
        return result


def _plan():
    return PlanRecord(
        "plan_1",
        "step",
        ["occt_cpp"],
        "ready",
        "now",
        process="injection",
        process_adapter_version="injection-geometry-core-v4",
        scope_id="injection.geometry-core",
        scope_version="4.0.0",
        input_ids=["input_1"],
        input_hashes={"input_1": "a" * 64},
        operations=[
            PlanOperation(
                "geometry.preflight",
                "geometry_preflight",
                required_artifacts=["preflight"],
            ),
            PlanOperation(
                "topology.index",
                "index_topology",
                depends_on=["geometry.preflight"],
                required_artifacts=["topology_map"],
            ),
            PlanOperation(
                "topology.aag",
                "build_aag",
                depends_on=["topology.index"],
                required_artifacts=["topology_map"],
            ),
            PlanOperation(
                "measure_draft",
                "measure_draft",
                depends_on=["topology.aag"],
                metric_ids=["injection.geometry.draft"],
                required_quantities=["draft_angle_deg"],
                required_artifacts=["topology_map", "scalar_field"],
                arguments={
                    "pull_direction": ResolvedArgument(
                        [0, 0, 1], "fact:pull_dir"
                    )
                },
                feature_refs=["feature.ordinary.1"],
                region_refs=["region.ordinary.1"],
            ),
            PlanOperation(
                "recognize_drilled_hole",
                "recognize_drilled_hole",
                depends_on=["topology.aag"],
                required_artifacts=["features"],
            ),
        ],
        regions=[
            RegionRecord(
                region_id="region.ordinary.1",
                input_sha256="a" * 64,
                coordinate_system="model",
                mode="whole_model",
                semantic_label="ordinary_model_region",
                source_refs=["test:fixture"],
                version="1.0.0",
                content_sha256="b" * 64,
                role="ordinary",
                feature_refs=["feature.ordinary.1"],
            )
        ],
    )


def test_capability_probe_is_cached_for_the_explicit_occt_analyzer(tmp_path):
    calls = []
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        capability_probe=lambda executable: calls.append(executable) or CAPABILITIES,
    )
    context = AnalyzerContext("dfm_1", tmp_path, "step", [])

    assert analyzer.capability(context).status is CapabilityStatus.AVAILABLE
    assert analyzer.capability(context).status is CapabilityStatus.AVAILABLE
    assert len(calls) == 1


def test_geometry_executable_discovery_prefers_explicit_config_then_path(
    monkeypatch, tmp_path
):
    configured = tmp_path / "configured" / "dfm-geometry.exe"
    configured.parent.mkdir()
    configured.write_bytes(b"")
    path_executable = tmp_path / "path" / "dfm-geometry.exe"
    path_executable.parent.mkdir()
    path_executable.write_bytes(b"")
    monkeypatch.setattr(
        "tools.dfm.analyzers.occt.shutil.which",
        lambda name: str(path_executable),
    )

    assert discover_geometry_executable(str(configured)) == str(configured.resolve())
    assert discover_geometry_executable("dfm-geometry-custom") == str(
        path_executable.resolve()
    )


def test_geometry_executable_relative_config_is_anchored_to_repo_root(
    monkeypatch, tmp_path
):
    repo_root = tmp_path / "checkout"
    module_path = repo_root / "tools" / "dfm" / "analyzers" / "occt.py"
    executable = (
        repo_root
        / "dfm-geometry"
        / "out"
        / "install"
        / "windows-vcpkg-vs2026-sln"
        / "bin"
        / "dfm-geometry.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    unrelated_cwd = tmp_path / "desktop-cwd"
    unrelated_cwd.mkdir()

    monkeypatch.setattr(occt_module, "__file__", str(module_path))
    monkeypatch.chdir(unrelated_cwd)

    configured = (
        "dfm-geometry/out/install/windows-vcpkg-vs2026-sln/"
        "bin/dfm-geometry.exe"
    )
    assert discover_geometry_executable(configured) == str(executable.resolve())


def test_capability_probe_rejects_incomplete_operation_registry(tmp_path):
    incompatible = {**CAPABILITIES, "operations": CAPABILITIES["operations"][:-1]}
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        capability_probe=lambda executable: incompatible,
    )

    capability = analyzer.capability(AnalyzerContext("dfm_1", tmp_path, "step", []))

    assert capability.status is CapabilityStatus.UNHEALTHY
    assert capability.error_code == "geometry_protocol_invalid"


def test_occt_analyzer_runs_versioned_request_and_returns_validated_artifacts(tmp_path):
    input_path = tmp_path / "inputs" / "part.step"
    input_path.parent.mkdir()
    input_path.write_bytes(b"opaque-step")
    input_record = InputRecord(
        "input_1", "step", "part.step", "inputs/part.step", 11, "a" * 64, "now"
    )
    runner = SuccessfulRunner()
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        runner=runner,
        capability_probe=lambda executable: CAPABILITIES,
        timeout_seconds=123,
    )
    context = AnalyzerContext(
        "dfm_1", tmp_path, "step", [input_record], "run_1", _plan()
    )

    artifacts = analyzer.run(context, CancellationToken())

    assert runner.argv[:3] == ["C:/dfm/dfm-geometry.exe", "analyze", "--request"]
    assert runner.request.contract_version == "dfm.geometry.request/v1"
    assert runner.request.task.schema_version == OBJECTIVE_SCHEMA_VERSION
    assert runner.request.task.regions == context.plan.regions
    assert runner.request.task.operations == context.plan.operations
    assert "verification_level" not in runner.request.task.to_dict()
    assert "assumed_pull_direction" not in runner.request.task.to_dict()
    assert {item.kind for item in artifacts} == {
        "preflight",
        "topology_map",
        "render_scene",
        "features",
        "measurements",
        "scalar_field",
        "worker_result",
    }


def test_occt_analyzer_accepts_audited_fixshape_normalization(tmp_path):
    input_path = tmp_path / "inputs" / "part.step"
    input_path.parent.mkdir()
    input_path.write_bytes(b"opaque-step")
    input_record = InputRecord(
        "input_1", "step", "part.step", "inputs/part.step", 11, "a" * 64, "now"
    )
    runner = SuccessfulRunner(healed=True)
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        runner=runner,
        capability_probe=lambda executable: CAPABILITIES,
    )

    artifacts = analyzer.run(
        AnalyzerContext("dfm_1", tmp_path, "step", [input_record], "run_1", _plan()),
        CancellationToken(),
    )

    assert {item.kind for item in artifacts} >= {"preflight", "topology_map"}


def test_missing_native_engine_is_an_explicit_dependency_error(tmp_path):
    analyzer = OcctAnalyzer(None)
    capability = analyzer.capability(AnalyzerContext("dfm_1", tmp_path, "step", []))

    assert capability.status is CapabilityStatus.DEPENDENCY_MISSING
    assert capability.error_code == "geometry_engine_missing"
    with pytest.raises(DFMError) as exc_info:
        analyzer.run(
            AnalyzerContext("dfm_1", tmp_path, "step", [], plan=_plan()),
            CancellationToken(),
        )
    assert exc_info.value.code == "geometry_engine_missing"


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("artifact_hash", "objective_artifact_invalid"),
        ("input_hash", "objective_result_invalid"),
        ("path_escape", "objective_result_invalid"),
        ("topology_ref", "objective_result_invalid"),
        ("topology_hash", "objective_result_invalid"),
        ("mesh_hash", "objective_result_invalid"),
        ("algorithm_version", "objective_result_invalid"),
        ("healing_audit", "objective_result_invalid"),
        ("duplicate_event", "geometry_protocol_invalid"),
        ("duplicate_kind", "objective_result_invalid"),
        ("manifest_result_path", "objective_result_invalid"),
        ("stdout_pollution", "geometry_protocol_invalid"),
        ("error_on_success", "geometry_protocol_invalid"),
    ],
)
def test_occt_analyzer_rejects_untrusted_worker_outputs(tmp_path, mode, expected_code):
    input_path = tmp_path / "inputs" / "part.step"
    input_path.parent.mkdir()
    input_path.write_bytes(b"opaque-step")
    input_record = InputRecord(
        "input_1", "step", "part.step", "inputs/part.step", 11, "a" * 64, "now"
    )
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        runner=TamperingRunner(mode),
        capability_probe=lambda executable: CAPABILITIES,
    )
    context = AnalyzerContext(
        "dfm_1", tmp_path, "step", [input_record], "run_1", _plan()
    )

    with pytest.raises(DFMError) as exc_info:
        analyzer.run(context, CancellationToken())

    assert exc_info.value.code == expected_code


def test_native_protocol_and_artifact_json_schemas_validate_real_adapter_shapes(
    tmp_path,
):
    schema_dir = Path("dfm-geometry/schemas")
    if not schema_dir.is_dir():
        pytest.skip("external DFMAnalysis_OCCT checkout is not available")
    schemas = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in schema_dir.glob("*.schema.json")
    }
    assert set(schemas) == {
        "capabilities.schema",
        "event.schema",
        "features.schema",
        "measurements.schema",
        "preflight.schema",
        "request.schema",
        "render_scene.schema",
        "result.schema",
        "scalar_field.schema",
        "topology_map.schema",
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)

    Draft202012Validator(schemas["capabilities.schema"]).validate(CAPABILITIES)
    input_path = tmp_path / "inputs" / "part.step"
    input_path.parent.mkdir()
    input_path.write_bytes(b"opaque-step")
    runner = SuccessfulRunner()
    analyzer = OcctAnalyzer(
        "C:/dfm/dfm-geometry.exe",
        runner=runner,
        capability_probe=lambda executable: CAPABILITIES,
    )
    analyzer.run(
        AnalyzerContext(
            "dfm_1",
            tmp_path,
            "step",
            [
                InputRecord(
                    "input_1",
                    "step",
                    "part.step",
                    "inputs/part.step",
                    11,
                    "a" * 64,
                    "now",
                )
            ],
            "run_1",
            _plan(),
        ),
        CancellationToken(),
    )
    Draft202012Validator(schemas["request.schema"]).validate(runner.request.to_dict())
    output = Path(runner.request.output_dir)
    for stem, filename in (
        ("preflight.schema", "preflight.json"),
        ("topology_map.schema", "topology_map.json"),
        ("render_scene.schema", "render_scene.json"),
        ("features.schema", "features.json"),
        ("measurements.schema", "measurements.json"),
        ("scalar_field.schema", "scalar_field_1.json"),
        ("result.schema", "engine_result.json"),
    ):
        Draft202012Validator(schemas[stem]).validate(
            json.loads((output / filename).read_text(encoding="utf-8"))
        )
    for event in (
        WorkerEvent(
            1,
            "progress",
            stage="objective_load",
            percent=5,
            contract_version=GEOMETRY_EVENT_CONTRACT,
        ),
        WorkerEvent(
            1,
            "artifact",
            kind="preflight",
            path="preflight.json",
            contract_version=GEOMETRY_EVENT_CONTRACT,
        ),
        WorkerEvent(
            1,
            "completed",
            path="engine_result.json",
            contract_version=GEOMETRY_EVENT_CONTRACT,
        ),
        WorkerEvent(
            1,
            "error",
            code="invalid_brep",
            message="invalid",
            contract_version=GEOMETRY_EVENT_CONTRACT,
        ),
    ):
        native_payload = {
            key: value for key, value in event.to_dict().items() if value is not None
        }
        Draft202012Validator(schemas["event.schema"]).validate(native_payload)
