from pathlib import Path

import pytest

from tools.dfm.contracts import (
    FeatureRecord,
    GeometryRef,
    InputRecord,
    ProjectManifest,
    RegionRecord,
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
    assert capability["output_contracts"] == ["FeatureRecord[]", "RegionRecord[]"]
    with pytest.raises(DFMError) as exc_info:
        provider.recognize(_input(), process="injection")

    assert exc_info.value.code == "unsupported_capability"


def test_occt_cpp_is_the_explicit_external_production_provider():
    provider = OCCTCppFeatureRecognitionProvider()
    capability = provider.capability()

    assert capability["status"] == "dependency_missing"
    assert capability["deployment"] == "external_project"
    assert capability["primary_production_target"] is True
    assert capability["supported_formats"] == ["step"]
    assert "GeometryDiscoveryResultManifest" in capability["output_contracts"]
    with pytest.raises(DFMError) as exc_info:
        provider.recognize(_input(), process="injection")

    assert exc_info.value.code == "unsupported_capability"


def test_feature_catalog_declares_executable_facts_and_honest_placeholder_fallback():
    engine = DiscoveryEngine()
    capability = engine.capability()

    assert engine.required_fact_names() == {"model_units"}
    assert capability["placeholder_policy"] == {
        "behavior": "treat_as_ordinary",
        "fallback_feature_kind": "ordinary_part",
        "fallback_region_role": "ordinary",
        "coverage": "whole_model",
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


def test_main_wall_region_exclusively_owns_wall_thickness_target():
    input_record = _input()
    topology_id = "topology.snapshot.main-wall"
    feature_id = "feature.main_wall.1"
    region_id = "region.main_wall.1.wall"
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
                region_refs=[region_id],
                recognizer="test",
                recognizer_version="1",
            )
        ],
        regions=[
            RegionRecord(
                region_id=region_id,
                input_sha256=input_record.sha256,
                coordinate_system="model",
                mode="topology_refs",
                semantic_label="main_wall",
                source_refs=["recognizer:test"],
                version="1",
                content_sha256="b" * 64,
                geometry_refs=[geometry_ref],
                role="wall",
                feature_refs=[feature_id],
            )
        ],
    )
    engine = DiscoveryEngine()
    refreshed, snapshot = engine.freeze(manifest)
    targets = engine.analysis_targets(refreshed, snapshot)

    wall_targets = [
        item
        for item in targets
        if item["metric_id"] == "injection.geometry.wall_thickness"
    ]
    assert len(wall_targets) == 1
    assert wall_targets[0]["feature"].kind == "main_wall"
    assert wall_targets[0]["region"].region_id == region_id
    ordinary = next(item for item in refreshed.regions if item.role == "ordinary")
    assert ordinary.mode == "topology_complement"
    assert ordinary.excluded_geometry_refs == [geometry_ref]
