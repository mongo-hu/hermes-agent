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
    DrawingFragment,
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
        fragments=[
            DrawingFragment(
                text="MATERIAL ABS",
                confidence=0.96,
                page=1,
                bbox=[10, 20, 30, 40],
            ),
            DrawingFragment(
                text="WALL 1.2 mm",
                confidence=0.91,
                page=1,
                bbox=[50, 60, 70, 80],
            ),
        ],
        raw_text="MATERIAL ABS\nWALL 1.2 mm",
        diagnostics={
            "ocr_fragment_count": 2,
            "semantic_interpretation": "hermes_agent_event_loop",
        },
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


def test_interface_returns_ocr_fragments_without_model_semantics(tmp_path, monkeypatch):
    source = tmp_path / "drawing.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(interface, "pipeline_capability", _available)

    def processor(*_args, **_kwargs):
        return (
            {
                "fragments": [
                    {
                        "text": "MATERIAL ABS",
                        "page": 2,
                        "bbox": [1, 2, 3, 4],
                        "confidence": 0.97,
                    }
                ],
                "diagnostics": {"semantic_interpretation": "hermes_agent_event_loop"},
            },
            "MATERIAL ABS",
        )

    result = interface.execute_2d_pipeline(str(source), processor=processor)

    assert result.fragments == [
        DrawingFragment(
            text="MATERIAL ABS",
            confidence=0.97,
            page=2,
            bbox=[1, 2, 3, 4],
        )
    ]
    serialized = json.dumps(result.to_dict())
    assert "candidate" not in serialized.lower()
    assert "model" not in serialized.lower()


def test_drawing_analyzer_emits_ocr_evidence_artifacts_and_events(tmp_path):
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
        "drawing_ocr_fragments",
        "drawing_diagnostics",
    }
    fragment_artifact = next(
        item for item in artifacts if item.kind == "drawing_ocr_fragments"
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / fragment_artifact.relative_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["fragment_id"].startswith("fragment.drawing.")
    assert rows[0]["input_id"] == record.input_id
    assert rows[0]["page"] == 1
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


def test_agent_fusion_proposal_is_program_validated_and_never_confirmed():
    observation = ObservationRecord(
        observation_id="observation.agent.wall",
        input_id="input_drawing_1",
        kind="wall_thickness",
        value=1.2,
        unit="mm",
        source_refs=["artifact:ocr#fragment=fragment.1"],
        confidence=0.9,
        provenance={
            "provider": "hermes_agent_event_loop",
            "source_type": "drawing_recognition",
        },
    )
    feature = FeatureRecord(
        feature_id="feature.ordinary.1",
        kind="ordinary_part",
        source_refs=["input:model"],
        confidence=1.0,
        input_sha256="a" * 64,
        region_refs=["region.ordinary.1"],
        properties={"fallback": True},
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
    manifest = ProjectManifest(
        project_id="dfm_123456789abc",
        name="Fusion",
        created_at="2026-08-27T00:00:00Z",
        updated_at="2026-08-27T00:00:00Z",
        inputs=[_input()],
        observations=[observation],
        features=[feature],
        regions=[region],
    )

    links = FusionAnalyzer().validate_agent_proposals(
        manifest,
        [
            {
                "observation_refs": [observation.observation_id],
                "feature_refs": [feature.feature_id],
                "region_refs": [region.region_id],
                "confidence": 0.8,
                "rationale": "The drawing explicitly labels the wall.",
            }
        ],
    )

    assert len(links) == 1
    assert links[0].status == "ambiguous"
    assert links[0].confidence == 0.8
    assert links[0].diagnostics["geometry_validation"] == "reference_only"
    assert links[0].diagnostics["requires_review"] is True


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


def test_mixed_input_uses_agent_observation_and_fusion_submission_flow(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(DrawingAnalyzer(pipeline=_result, capability_probe=_available))
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    service = DFMService(
        config=DFMConfig(),
        workspace=DFMWorkspace(tmp_path / "workspace"),
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
        drawing_input = service.project(
            "add_input", project_id=project_id, path=str(drawing_path)
        )["input"]
        for fact_name, fact_value in {
            "process": "injection",
            "model_units": "mm",
        }.items():
            service.project(
                "confirm_fact",
                project_id=project_id,
                fact_name=fact_name,
                fact_value=fact_value,
            )

        pending = service.analysis("discover", project_id=project_id)
        assert pending["status"] == "agent_interpretation_required"

        drawing_context = service.analysis(
            "drawing_context",
            project_id=project_id,
            input_id=drawing_input["input_id"],
        )
        material_fragment, wall_fragment = drawing_context["fragments"]
        submitted = service.analysis(
            "submit_observations",
            project_id=project_id,
            input_id=drawing_input["input_id"],
            expected_revision=drawing_context["revision"],
            observations=[
                {
                    "kind": "material",
                    "value": "ABS",
                    "confidence": 0.95,
                    "source_fragment_refs": [material_fragment["fragment_id"]],
                },
                {
                    "kind": "wall_thickness",
                    "value": 1.2,
                    "unit": "mm",
                    "confidence": 0.9,
                    "source_fragment_refs": [wall_fragment["fragment_id"]],
                },
            ],
        )
        material = next(
            item for item in submitted["observations"] if item["kind"] == "material"
        )
        assert material["status"] == "needs_confirmation"
        assert material["provenance"]["provider"] == "hermes_agent_event_loop"

        fusion_pending = service.analysis("discover", project_id=project_id)
        assert fusion_pending["status"] == "agent_fusion_required"
        fusion_context = service.analysis("fusion_context", project_id=project_id)
        wall = next(
            item
            for item in fusion_context["observations"]
            if item["kind"] == "wall_thickness"
        )
        feature = fusion_context["features"][0]
        region = fusion_context["regions"][0]
        fusion_submission = service.analysis(
            "submit_fusion_links",
            project_id=project_id,
            expected_revision=fusion_context["revision"],
            fusion_links=[
                {
                    "observation_refs": [wall["observation_id"]],
                    "feature_refs": [feature["feature_id"]],
                    "region_refs": [region["region_id"]],
                    "confidence": 0.8,
                    "rationale": "Explicit wall callout.",
                }
            ],
        )
        assert fusion_submission["fusion_links"][0]["status"] == "ambiguous"

        discovery = service.analysis("discover", project_id=project_id)
        assert discovery["ok"] is True
        assert {item["kind"] for item in discovery["observations"]} == {
            "material",
            "wall_thickness",
        }
        assert len(discovery["fusion_links"]) == 1
        assert discovery["drawing_discovery"]["status"] == "completed"

        for fact_name, fact_value in {
            "material": "ABS",
            "pull_dir": [0, 0, 1],
        }.items():
            service.project(
                "confirm_fact",
                project_id=project_id,
                fact_name=fact_name,
                fact_value=fact_value,
            )
        plan = service.analysis("plan", project_id=project_id)

        assert plan["plan"]["input_mode"] == "fusion"
        assert plan["plan"]["analyzer_keys"] == ["step"]
        assert plan["plan"]["operations"]
    finally:
        service.close()


def test_agent_observation_rejects_evidence_from_outside_context(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(DrawingAnalyzer(pipeline=_result, capability_probe=_available))
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    service = DFMService(
        workspace=DFMWorkspace(tmp_path / "workspace"),
        registry=registry,
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="Evidence validation")["project_id"]
        drawing_path = tmp_path / "drawing.png"
        drawing_path.write_bytes(b"png")
        drawing = service.project(
            "add_input", project_id=project_id, path=str(drawing_path)
        )["input"]
        context = service.analysis(
            "drawing_context", project_id=project_id, input_id=drawing["input_id"]
        )

        with pytest.raises(DFMError) as exc_info:
            service.analysis(
                "submit_observations",
                project_id=project_id,
                input_id=drawing["input_id"],
                expected_revision=context["revision"],
                observations=[
                    {
                        "kind": "material",
                        "value": "ABS",
                        "confidence": 0.9,
                        "source_fragment_refs": ["fragment.fabricated"],
                    }
                ],
            )

        assert exc_info.value.code == "observation_evidence_invalid"
    finally:
        service.close()
