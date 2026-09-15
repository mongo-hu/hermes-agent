import json
from pathlib import Path

import pytest

from tools.dfm.backends.contracts import (
    BACKEND_CAPABILITY_SCHEMA_VERSION,
    GeometryBackendCapability,
    GeometryCalculatorCapability,
    GeometryRecognizerCapability,
)
from tools.dfm.contracts import (
    DISCOVERY_SCHEMA_VERSION,
    OBJECTIVE_SCHEMA_VERSION,
    FeatureRecord,
    GeometryDiscoveryResultManifest,
    GeometryDiscoveryTaskRequest,
    GeometryRef,
    ObjectiveArtifactManifest,
    ObservationRecord,
    RecognizerExecutionResult,
    RegionRecord,
    ResolvedArgument,
)


SCHEMA_ROOT = Path(__file__).parents[3] / "tools" / "dfm" / "schemas"


def _validate(name: str, payload: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_objective_artifacts_and_topology_indices_match_the_published_contracts():
    jsonschema = pytest.importorskip("jsonschema")
    objective = json.loads(
        (SCHEMA_ROOT / "objective_task.schema.json").read_text(encoding="utf-8")
    )
    allowed = objective["properties"]["operations"]["items"]["properties"][
        "required_artifacts"
    ]["items"]["enum"]
    assert "feature_set" not in allowed
    assert "region_set" not in allowed

    for schema_name in (
        "measurement.schema.json",
        "topology_map.schema.json",
        "scalar_field.schema.json",
        "evidence_geometry.schema.json",
        "evidence_record.schema.json",
    ):
        schema = json.loads((SCHEMA_ROOT / schema_name).read_text(encoding="utf-8"))
        encoded = json.dumps(schema)
        assert not any(
            match in encoded
            for match in ('"index": {"type": "integer", "minimum": 0}',)
        ), schema_name
        jsonschema.Draft202012Validator.check_schema(schema)


def _artifact(artifact_id: str, kind: str, filename: str) -> ObjectiveArtifactManifest:
    return ObjectiveArtifactManifest(
        artifact_id=artifact_id,
        kind=kind,
        filename=filename,
        media_type="application/json",
        size_bytes=10,
        sha256="c" * 64,
    )


def test_geometry_discovery_task_round_trips_and_matches_schema():
    task = GeometryDiscoveryTaskRequest(
        schema_version=DISCOVERY_SCHEMA_VERSION,
        request_id="discovery.request.1",
        input_id="input_step_1",
        input_sha256="a" * 64,
        input_format="step",
        process="injection",
        recognizer_ids=["injection-main-wall", "injection-screw-boss"],
        facts={
            "model_units": ResolvedArgument("mm", "fact:model_units"),
            "process": ResolvedArgument("injection", "fact:process"),
        },
    )

    payload = task.to_dict()
    assert GeometryDiscoveryTaskRequest.from_dict(payload) == task
    _validate("geometry_discovery_task.schema.json", payload)


def test_geometry_discovery_result_round_trips_and_matches_schema():
    topology_snapshot_id = "topology.snapshot.1"
    region_id = "region.screw_boss.1.wall"
    feature_id = "feature.screw_boss.1"
    observation_id = "observation.pull_direction.1"
    region = RegionRecord(
        region_id=region_id,
        input_sha256="a" * 64,
        coordinate_system="model",
        mode="topology_refs",
        semantic_label="screw_boss_wall",
        source_refs=["recognizer:occt_cpp@1"],
        version="1",
        content_sha256="b" * 64,
        role="wall",
        feature_refs=[feature_id],
        geometry_refs=[
            GeometryRef(
                "face",
                7,
                "a" * 64,
                topology_snapshot_id=topology_snapshot_id,
                entity_id="face_000007",
            )
        ],
    )
    feature = FeatureRecord(
        feature_id=feature_id,
        kind="screw_boss",
        source_refs=["recognizer:occt_cpp@1"],
        confidence=0.96,
        input_sha256="a" * 64,
        region_refs=[region_id],
        recognizer="occt_cpp_feature_recognition",
        recognizer_version="1",
    )
    observation = ObservationRecord(
        observation_id=observation_id,
        input_id="input_step_1",
        kind="pull_direction_candidate",
        value=[0, 0, 1],
        source_refs=["recognizer:occt_cpp@1"],
        confidence=0.85,
        status="candidate",
        provenance={"backend": "occt_cpp", "algorithm_version": "1"},
    )
    result = GeometryDiscoveryResultManifest(
        schema_version=DISCOVERY_SCHEMA_VERSION,
        producer_version="occt-cpp-engine-1",
        request_id="discovery.request.1",
        input_id="input_step_1",
        input_sha256="a" * 64,
        process="injection",
        topology_snapshot_id=topology_snapshot_id,
        render_mesh_snapshot_id="mesh.snapshot.1",
        geometry_snapshot_ref="geometry_snapshot",
        observations=[observation],
        features=[feature],
        regions=[region],
        recognizers=[
            RecognizerExecutionResult(
                recognizer_id="injection-screw-boss",
                status="completed",
                implementation_version="1",
                feature_refs=[feature_id],
                region_refs=[region_id],
                observation_refs=[observation_id],
            )
        ],
        artifacts=[
            _artifact("geometry_snapshot", "geometry_snapshot", "geometry.brep"),
            _artifact("topology_map", "topology_map", "topology.json"),
            _artifact("render_scene", "render_scene", "scene.json"),
        ],
    )

    payload = result.to_dict()
    assert GeometryDiscoveryResultManifest.from_dict(payload) == result
    _validate("geometry_discovery_result.schema.json", payload)


def test_geometry_discovery_rejects_cross_snapshot_region_refs():
    region = RegionRecord(
        region_id="region.1",
        input_sha256="a" * 64,
        coordinate_system="model",
        mode="topology_refs",
        semantic_label="wall",
        source_refs=["recognizer:occt_cpp@1"],
        version="1",
        content_sha256="b" * 64,
        role="wall",
        feature_refs=["feature.1"],
        geometry_refs=[
            GeometryRef(
                "face",
                1,
                "a" * 64,
                topology_snapshot_id="topology.other",
                entity_id="face_000001",
            )
        ],
    )
    feature = FeatureRecord(
        "feature.1",
        "main_wall",
        ["recognizer:occt_cpp@1"],
        1.0,
        input_sha256="a" * 64,
        region_refs=["region.1"],
        recognizer="occt_cpp_feature_recognition",
        recognizer_version="1",
    )

    with pytest.raises(ValueError, match="another snapshot"):
        GeometryDiscoveryResultManifest(
            schema_version=DISCOVERY_SCHEMA_VERSION,
            producer_version="occt-cpp-engine-1",
            request_id="discovery.request.1",
            input_id="input_step_1",
            input_sha256="a" * 64,
            process="injection",
            topology_snapshot_id="topology.expected",
            render_mesh_snapshot_id="mesh.snapshot.1",
            geometry_snapshot_ref="geometry_snapshot",
            features=[feature],
            regions=[region],
            recognizers=[
                RecognizerExecutionResult(
                    "injection-main-wall",
                    "completed",
                    feature_refs=["feature.1"],
                    region_refs=["region.1"],
                )
            ],
            artifacts=[
                _artifact("geometry_snapshot", "geometry_snapshot", "geometry.brep"),
                _artifact("topology_map", "topology_map", "topology.json"),
                _artifact("render_scene", "render_scene", "scene.json"),
            ],
        )


def test_blocked_recognizer_requires_explicit_missing_facts():
    with pytest.raises(ValueError, match="must name missing facts"):
        RecognizerExecutionResult("injection-undercut", "blocked")


def test_discovery_geometry_snapshot_ref_must_name_geometry_artifact():
    with pytest.raises(ValueError, match="incomplete or ambiguous"):
        GeometryDiscoveryResultManifest(
            schema_version=DISCOVERY_SCHEMA_VERSION,
            producer_version="occt-cpp-engine-1",
            request_id="discovery.request.1",
            input_id="input_step_1",
            input_sha256="a" * 64,
            process="injection",
            topology_snapshot_id="topology.snapshot.1",
            render_mesh_snapshot_id="mesh.snapshot.1",
            geometry_snapshot_ref="topology_map",
            recognizers=[
                RecognizerExecutionResult(
                    "injection-pull-direction",
                    "blocked",
                    missing_fact_names=["pull_dir"],
                )
            ],
            artifacts=[
                _artifact("geometry_snapshot", "geometry_snapshot", "geometry.brep"),
                _artifact("topology_map", "topology_map", "topology.json"),
                _artifact("render_scene", "render_scene", "scene.json"),
            ],
        )


def test_backend_capability_separates_recognizers_and_calculators():
    capability = GeometryBackendCapability(
        schema_version=BACKEND_CAPABILITY_SCHEMA_VERSION,
        status="available",
        backend_id="occt_cpp",
        backend_version="engine-1",
        formats={"step": "experimental", "parasolid_xt": "not_implemented"},
        recognizers={
            "injection-main-wall": GeometryRecognizerCapability(
                status="experimental",
                implementation_version="recognizer-1",
                required_fact_names=("process", "model_units"),
                output_observation_kinds=(),
                output_feature_kinds=("main_wall",),
                output_region_roles=("wall",),
                supported_formats=("step",),
            )
        },
        calculators={
            "measure_draft": GeometryCalculatorCapability(
                status="experimental",
                contract_version=OBJECTIVE_SCHEMA_VERSION,
                implementation_version="draft-1",
                required_arguments=("pull_direction",),
                output_quantities=("draft_angle_deg",),
                output_artifact_kinds=(
                    "scalar_field",
                    "render_scene",
                    "topology_map",
                ),
                supported_formats=("step",),
                supported_region_modes=("topology_refs", "topology_complement"),
            )
        },
    )

    payload = capability.to_dict()
    assert GeometryBackendCapability.from_dict(payload) == capability
    _validate("geometry_backend_capability.schema.json", payload)


def test_certified_backend_capability_requires_certification_hash():
    with pytest.raises(ValueError, match="implementation capability"):
        GeometryRecognizerCapability(
            status="certified",
            implementation_version="recognizer-1",
            supported_formats=("step",),
        )
