import json
from pathlib import Path

import pytest

from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.drawing import DrawingAnalyzer
from tools.dfm.analyzers.fusion import FusionAnalyzer
from tools.dfm.analyzers.parasolid import ParasolidAnalyzer
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.analyzers.step import StepAnalyzer
from tools.dfm.config import DFMConfig
from tools.dfm.contracts import (
    FeatureRecord,
    InputRecord,
    ObservationRecord,
    ProjectManifest,
    RegionRecord,
    WorkerEvent,
)
from tools.dfm.drawing_pipeline import interface
from tools.dfm.drawing_pipeline.interface import (
    DrawingCandidate,
    DrawingPipelineError,
    DrawingPipelineResult,
)
from tools.dfm.errors import DFMError
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.service import DFMService


STEP_PAYLOAD = (
    Path(__file__).parents[3]
    / "tests"
    / "fixtures"
    / "dfm"
    / "step"
    / "injection_plate_with_hole.step"
).read_bytes()


def _available(_suffixes=None):
    return {
        "available": True,
        "missing": [],
        "unsupported_formats": [],
        "supported_formats": [".jpeg", ".jpg", ".pdf", ".png"],
        "provider_version": "test",
    }


def _result(*_args, **_kwargs):
    return DrawingPipelineResult(
        provider="hermes_drawing_pipeline",
        provider_version="2.0.0",
        candidates=[
            DrawingCandidate(
                kind="material",
                value="ABS",
                confidence=0.96,
                page=1,
                bbox=[10, 20, 30, 40],
                original_text="MATERIAL ABS",
            ),
            DrawingCandidate(
                kind="minimum_wall_thickness",
                value=1.2,
                unit="mm",
                confidence=0.91,
                page=1,
                bbox=[50, 60, 70, 80],
                original_text="WALL 1.2",
                feature_kind="ordinary_part",
                region_role="ordinary",
            ),
        ],
        raw_text="MATERIAL ABS\nWALL 1.2",
        diagnostics={"semantic_extraction": {"status": "completed"}},
    )


def _input(index: int = 1) -> InputRecord:
    digest = f"{index:064x}"
    return InputRecord(
        input_id=f"input_drawing_{index}",
        kind="drawing",
        source_name=f"drawing-{index}.png",
        relative_path=f"inputs/drawing-{index}.png",
        size_bytes=3,
        sha256=digest,
        created_at="2026-08-27T00:00:00Z",
        format_id="drawing",
        representation="document",
    )


def test_interface_returns_candidates_not_lifecycle_events(tmp_path, monkeypatch):
    source = tmp_path / "drawing.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(interface, "pipeline_capability", _available)

    def processor(*_args, **_kwargs):
        return (
            {
                "extraction": {"material": "ABS"},
                "fragments": [
                    {
                        "text": "MATERIAL ABS",
                        "page": 2,
                        "bbox": [1, 2, 3, 4],
                        "confidence": 0.97,
                    }
                ],
                "diagnostics": {"semantic_extraction": {"status": "completed"}},
            },
            "MATERIAL ABS",
        )

    result = interface.execute_2d_pipeline(str(source), processor=processor)

    assert result.candidates == [
        DrawingCandidate(
            kind="material",
            value="ABS",
            confidence=0.97,
            page=2,
            bbox=[1, 2, 3, 4],
            original_text="MATERIAL ABS",
        )
    ]
    assert "Raw_Extracted_Text" not in json.dumps(result.to_dict())


def test_drawing_analyzer_emits_formal_observations_artifacts_and_events(tmp_path):
    record = _input()
    source = tmp_path / record.relative_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    events = []
    context = AnalyzerContext(
        "dfm_123456789abc",
        tmp_path,
        "drawing",
        [record],
        "run_drawing",
        event_sink=events.append,
    )
    analyzer = DrawingAnalyzer(pipeline=_result, capability_probe=_available)

    artifacts = analyzer.run(context, CancellationToken())

    assert {item.kind for item in artifacts} == {
        "drawing_raw_text",
        "drawing_observations",
        "drawing_diagnostics",
    }
    observation_artifact = next(
        item for item in artifacts if item.kind == "drawing_observations"
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / observation_artifact.relative_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["observation_id"].startswith("observation.drawing.")
    assert rows[0]["source_refs"][0].startswith("artifact:artifact_drawing-raw-text")
    assert rows[0]["provenance"]["page"] == 1
    assert all(WorkerEvent.from_dict(item.to_dict()) for item in events)
    assert events[-1].type == "completed"


def test_drawing_pipeline_errors_are_not_materialized_as_success(tmp_path):
    record = _input()
    source = tmp_path / record.relative_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    events = []

    def failing(*_args, **_kwargs):
        raise DrawingPipelineError("drawing_ocr_failed", "OCR failed")

    analyzer = DrawingAnalyzer(pipeline=failing, capability_probe=_available)
    context = AnalyzerContext(
        "dfm_123456789abc",
        tmp_path,
        "drawing",
        [record],
        "run_failed",
        event_sink=events.append,
    )

    with pytest.raises(DFMError) as exc_info:
        analyzer.run(context, CancellationToken())

    assert exc_info.value.code == "drawing_ocr_failed"
    assert events[-1].type == "error"
    assert not list((tmp_path / "runs" / "run_failed").rglob("*.jsonl"))


def test_fusion_resolver_only_links_local_reviewable_observations():
    record = _input()
    feature = FeatureRecord(
        feature_id="feature.ordinary.1",
        kind="ordinary_part",
        source_refs=["input:model"],
        confidence=1.0,
        input_sha256="a" * 64,
        region_refs=["region.ordinary.1"],
    )
    region = RegionRecord(
        region_id="region.ordinary.1",
        input_sha256="a" * 64,
        coordinate_system="model",
        mode="whole_model",
        semantic_label="ordinary",
        source_refs=["input:model"],
        version="1",
        content_sha256="b" * 64,
        role="ordinary",
        feature_refs=[feature.feature_id],
    )
    observations = []
    for candidate in _result().candidates:
        observations.append(
            ObservationRecord(
                observation_id=f"observation.{candidate.kind}",
                input_id=record.input_id,
                kind=candidate.kind,
                value=candidate.value,
                source_refs=["drawing:page=1"],
                confidence=candidate.confidence,
                unit=candidate.unit,
                provenance={
                    "feature_kind": candidate.feature_kind,
                    "region_role": candidate.region_role,
                },
            )
        )
    manifest = ProjectManifest(
        project_id="dfm_123456789abc",
        name="Fusion",
        created_at="2026-08-27T00:00:00Z",
        updated_at="2026-08-27T00:00:00Z",
        observations=observations,
        features=[feature],
        regions=[region],
    )

    links = FusionAnalyzer().resolve(manifest)

    assert len(links) == 1
    assert links[0].observation_refs == ["observation.minimum_wall_thickness"]
    assert links[0].feature_refs == [feature.feature_id]
    assert links[0].region_refs == [region.region_id]
    assert links[0].status == "candidate"


def test_configured_geometry_backend_does_not_silently_fall_back(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(DrawingAnalyzer(pipeline=_result, capability_probe=_available))
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    service = DFMService(
        config=DFMConfig(geometry_backend="occt_cpp"),
        workspace=DFMWorkspace(tmp_path / "workspace"),
        registry=registry,
        reconcile_jobs=False,
    )
    manifest = ProjectManifest(
        project_id="dfm_123456789abc",
        name="Configured geometry",
        created_at="2026-08-27T00:00:00Z",
        updated_at="2026-08-27T00:00:00Z",
        inputs=[
            InputRecord(
                input_id="input_step_1",
                kind="step",
                source_name="part.step",
                relative_path="inputs/part.step",
                size_bytes=3,
                sha256="a" * 64,
                created_at="2026-08-27T00:00:00Z",
                format_id="step",
                representation="brep",
            )
        ],
        input_mode="step",
    )
    try:
        analyzer_key = service._objective_analyzer_key(manifest)

        assert analyzer_key == "occt_cpp"
        with pytest.raises(DFMError) as exc_info:
            registry.get(analyzer_key)
        assert exc_info.value.code == "analyzer_not_found"
    finally:
        service.close()


def test_mixed_input_discovery_persists_observations_fusion_and_routes_geometry(
    tmp_path,
):
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(DrawingAnalyzer(pipeline=_result, capability_probe=_available))
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    workspace = DFMWorkspace(tmp_path / "workspace")
    service = DFMService(
        config=DFMConfig(),
        workspace=workspace,
        registry=registry,
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="Mixed")["project_id"]
        step_path = tmp_path / "part.step"
        drawing_path = tmp_path / "drawing.png"
        step_path.write_bytes(STEP_PAYLOAD)
        drawing_path.write_bytes(b"png")
        service.project("add_input", project_id=project_id, path=str(step_path))
        service.project("add_input", project_id=project_id, path=str(drawing_path))
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="process",
            fact_value="injection",
        )
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="model_units",
            fact_value="mm",
        )

        discovery = service.analysis("discover", project_id=project_id)

        assert discovery["ok"] is True
        assert {item["kind"] for item in discovery["observations"]} == {
            "material",
            "minimum_wall_thickness",
        }
        material = next(
            item for item in discovery["observations"] if item["kind"] == "material"
        )
        assert material["status"] == "needs_confirmation"
        assert len(discovery["fusion_links"]) == 1
        assert discovery["fusion_links"][0]["status"] == "candidate"
        assert discovery["drawing_discovery"]["status"] == "completed"

        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="material",
            fact_value="ABS",
        )
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="pull_dir",
            fact_value=[0, 0, 1],
        )
        plan = service.analysis("plan", project_id=project_id)

        assert plan["plan"]["input_mode"] == "fusion"
        assert plan["plan"]["analyzer_keys"] == ["step"]
        assert plan["plan"]["operations"]
    finally:
        service.close()
