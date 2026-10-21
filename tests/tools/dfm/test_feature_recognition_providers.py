from pathlib import Path

import pytest

from tools.dfm.contracts import (
    FeatureRecord,
    GeometryRef,
    InputRecord,
    ProjectManifest,
)
from tools.dfm.discovery import DiscoveryEngine
from tools.dfm.errors import DFMError
from tools.dfm.feature_recognition import (
    MTKFeatureRecognitionProvider,
    NXFeatureRecognitionProvider,
    OCCTCppFeatureRecognitionProvider,
)


def _input() -> InputRecord:
    return InputRecord(
        input_id="input_step_1",
        kind="step",
        source_name="part.step",
        relative_path="inputs/part.step",
        size_bytes=1,
        sha256="a" * 64,
        created_at="now",
        format_id="step",
        representation="brep",
    )


@pytest.mark.parametrize(
    "provider", [NXFeatureRecognitionProvider(), MTKFeatureRecognitionProvider()]
)
def test_pending_feature_providers_are_explicit_non_executing_placeholders(provider):
    capability = provider.capability()

    assert capability["status"] == "not_implemented"
    assert "process" in capability["required_fact_names"]
    assert "model_units" in capability["required_fact_names"]
    assert capability["output_contracts"] == ["FeatureRecord[]"]
    with pytest.raises(DFMError) as exc_info:
        provider.recognize(_input(), process="injection")

    assert exc_info.value.code == "unsupported_capability"


def test_occt_cpp_is_the_explicit_external_production_provider():
    provider = OCCTCppFeatureRecognitionProvider()
    capability = provider.capability()

    assert capability["status"] == "dependency_missing"
    assert capability["deployment"] == "persistent_external_worker"
    assert capability["primary_production_target"] is True
    assert capability["supported_formats"] == ["step"]
    assert "dfm.geometry/v2" in capability["output_contracts"]
    with pytest.raises(DFMError) as exc_info:
        provider.recognize(_input(), process="injection")

    assert exc_info.value.code == "discovery_workspace_required"


def test_occt_cpp_discovery_rejects_a_short_lived_session_fallback(tmp_path):
    provider = OCCTCppFeatureRecognitionProvider("C:/dfm/dfm-geometry.exe")
    input_record = _input()
    input_path = tmp_path / input_record.relative_path
    input_path.parent.mkdir(parents=True)
    input_path.write_bytes(b"step")

    assert provider.capability("dfm_project")["error_code"] == (
        "geometry_session_required"
    )
    with pytest.raises(DFMError) as exc_info:
        provider.recognize(
            input_record,
            process="injection",
            project_dir=tmp_path,
        )

    assert exc_info.value.code == "geometry_session_required"


def test_feature_catalog_declares_executable_facts_and_honest_placeholder_fallback():
    engine = DiscoveryEngine()
    capability = engine.capability()

    assert engine.required_fact_names() == {"model_units"}
    assert capability["status"] == "dependency_missing"
    assert capability["placeholder_policy"] == {
        "behavior": "fail_closed",
        "emit_synthetic_process_features": False,
    }
    placeholders = [
        item for item in capability["recognizers"] if item["status"] == "placeholder"
    ]
    assert placeholders
    assert all(item["required_fact_names"] for item in placeholders)
    pull_candidate = next(
        item
        for item in placeholders
        if item["recognizer_id"] == "injection-pull-direction-candidate"
    )
    assert pull_candidate["observation_kinds"] == ["pull_direction_candidate"]
    assert "pull_dir" not in pull_candidate["required_fact_names"]
    undercut = next(
        item
        for item in placeholders
        if item["recognizer_id"] == "injection-undercut-side-action"
    )
    assert "pull_dir" in undercut["required_fact_names"]
    assert capability["providers"]["occt_cpp_feature_recognition"].endswith(
        ":dependency_missing"
    )
    assert "nx_feature_recognition" not in capability["providers"]
    assert "mtk_feature_recognition" not in capability["providers"]


def test_feature_geometry_is_frozen_without_constructing_regions():
    input_record = _input()
    topology_id = "topology.snapshot.main-wall"
    feature_id = "feature.main_wall.1"
    geometry_ref = GeometryRef(
        "face",
        1,
        input_record.sha256,
        topology_snapshot_id=topology_id,
        entity_id="face-1",
    )
    manifest = ProjectManifest(
        project_id="dfm_aaaaaaaaaaaa",
        name="main wall",
        created_at="now",
        updated_at="now",
        inputs=[input_record],
        features=[
            FeatureRecord(
                feature_id=feature_id,
                kind="main_wall",
                source_refs=["recognizer:test"],
                confidence=0.9,
                input_sha256=input_record.sha256,
                properties={"geometry_refs": [geometry_ref.to_dict()]},
                recognizer="test",
                recognizer_version="1",
            )
        ],
    )
    engine = DiscoveryEngine()
    refreshed, snapshot = engine.freeze(manifest)

    assert refreshed.regions == []
    assert snapshot.feature_refs == [feature_id]
    assert snapshot.region_refs == []
    assert refreshed.features[0].properties["geometry_refs"] == [
        geometry_ref.to_dict()
    ]


def test_one_feature_carries_geometry_once_without_measurement_role_regions():
    input_record = _input()
    feature_id = "feature.screw_boss.1"
    geometry_ref = GeometryRef(
        "face",
        209,
        input_record.sha256,
        topology_snapshot_id="topology.snapshot.screw-boss",
        entity_id="face-209",
    )
    manifest = ProjectManifest(
        project_id="dfm_bbbbbbbbbbbb",
        name="overlapping screw-boss measurement roles",
        created_at="now",
        updated_at="now",
        inputs=[input_record],
        features=[
            FeatureRecord(
                feature_id=feature_id,
                kind="screw_boss",
                source_refs=["recognizer:occt_cpp"],
                confidence=0.9,
                input_sha256=input_record.sha256,
                properties={"geometry_refs": [geometry_ref.to_dict()]},
                recognizer="occt_cpp",
                recognizer_version="1",
            )
        ],
    )
    engine = DiscoveryEngine()
    refreshed, snapshot = engine.freeze(manifest)

    assert refreshed.regions == []
    assert snapshot.region_refs == []
    assert snapshot.feature_refs == [feature_id]
    assert refreshed.features[0].properties["geometry_refs"] == [
        geometry_ref.to_dict()
    ]
