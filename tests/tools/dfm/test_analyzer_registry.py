from pathlib import Path

import pytest

from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.registry import AnalyzerRegistry, build_default_registry
from tools.dfm.contracts import CapabilityStatus
from tools.dfm.config import DFMConfig
from tools.dfm.errors import DFMError


class AvailableAnalyzer:
    key = "test"
    version = "1"
    supported_inputs = ("step",)

    def capability(self, context):
        from tools.dfm.contracts import Capability

        return Capability(self.key, CapabilityStatus.AVAILABLE, "test analyzer")

    def run(self, context, cancellation):
        cancellation.raise_if_cancelled()
        return []


def _context(tmp_path):
    return AnalyzerContext("dfm_123", Path(tmp_path), "step", [])


def test_registry_rejects_duplicates_and_returns_deterministic_keys(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(AvailableAnalyzer())

    with pytest.raises(DFMError) as exc_info:
        registry.register(AvailableAnalyzer())

    assert exc_info.value.code == "analyzer_duplicate"
    assert registry.keys() == ["test"]
    assert (
        registry.get("test").capability(_context(tmp_path)).status
        is CapabilityStatus.AVAILABLE
    )


def test_default_registry_exposes_geometry_and_document_boundaries(tmp_path):
    registry = build_default_registry()
    context = _context(tmp_path)

    capabilities = {
        key: registry.get(key).capability(context) for key in registry.keys()
    }

    assert registry.keys() == ["drawing", "fusion", "parasolid", "step"]
    assert capabilities["step"].status in {
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.DEPENDENCY_MISSING,
    }
    assert capabilities["drawing"].status is CapabilityStatus.AVAILABLE
    assert capabilities["fusion"].status is CapabilityStatus.AVAILABLE
    assert capabilities["drawing"].details["applicable"] is False
    assert capabilities["fusion"].details["applicable"] is False


def test_default_registry_propagates_runtime_configuration():
    config = DFMConfig(
        runtime_python="C:/dfm/python.exe",
        timeout_seconds=123,
        drawing_model="drawing-model",
        drawing_base_url="https://models.example/v1",
        drawing_request_timeout_seconds=45,
    )

    registry = build_default_registry(config)
    analyzer = registry.get("step")
    drawing = registry.get("drawing")

    assert analyzer.python_executable == "C:/dfm/python.exe"
    assert analyzer.timeout_seconds == 123
    assert drawing.model_name == "drawing-model"
    assert drawing.base_url == "https://models.example/v1"
    assert drawing.timeout_seconds == 45


@pytest.mark.parametrize("key", ["drawing", "fusion"])
def test_document_analyzers_require_applicable_inputs(tmp_path, key):
    analyzer = build_default_registry().get(key)

    with pytest.raises(DFMError) as exc_info:
        analyzer.run(_context(tmp_path), CancellationToken())

    assert exc_info.value.code == "input_required"


def test_step_analyzer_requires_dependency_then_persisted_plan(tmp_path):
    analyzer = build_default_registry().get("step")

    with pytest.raises(DFMError) as exc_info:
        analyzer.run(_context(tmp_path), CancellationToken())

    expected = (
        "plan_required"
        if analyzer.capability(_context(tmp_path)).status is CapabilityStatus.AVAILABLE
        else "dependency_missing"
    )
    assert exc_info.value.code == expected


def test_cancellation_token_is_cooperative():
    token = CancellationToken()
    token.cancel()

    with pytest.raises(DFMError) as exc_info:
        token.raise_if_cancelled()

    assert exc_info.value.code == "run_cancelled"
