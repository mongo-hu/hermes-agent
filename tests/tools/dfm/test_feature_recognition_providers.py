from pathlib import Path

import pytest

from tools.dfm.contracts import InputRecord
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

    assert capability["status"] == "not_implemented"
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
        ":not_implemented"
    )
    assert "nx_feature_recognition" not in capability["providers"]
    assert "mtk_feature_recognition" not in capability["providers"]
