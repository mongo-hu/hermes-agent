import json
import threading
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
    DrawingCrop,
    DrawingImage,
    DrawingPage,
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
        provider="hermes_semantic_crop_pipeline",
        provider_version="3.0.0",
        pages=[DrawingPage(page=1, width=1000.0, height=800.0)],
        diagnostics={
            "page_count": 1,
            "ocr_used": False,
            "semantic_interpretation": "hermes_agent_event_loop",
        },
    )


def _overviews(*_args, **_kwargs):
    return [
        DrawingImage(
            page=1,
            pixel_width=1000,
            pixel_height=800,
            png_bytes=b"overview-png",
        )
    ]


def _crops(_path, regions, **_kwargs):
    return [
        DrawingCrop(
            page=int(item["page"]),
            pixel_width=400,
            pixel_height=240,
            png_bytes=f"crop-{item['region_id']}".encode(),
            region_id=str(item["region_id"]),
            type=str(item["type"]),
            bbox_1000=list(item["bbox_1000"]),
            decision=str(item["decision"]),
            reason=str(item.get("reason") or ""),
        )
        for item in regions
    ]


def _drawing_analyzer():
    return DrawingAnalyzer(
        pipeline=_result,
        overview_renderer=_overviews,
        crop_renderer=_crops,
        capability_probe=_available,
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


def test_interface_returns_page_metadata_without_ocr_or_model_client(
    tmp_path, monkeypatch
):
    source = tmp_path / "drawing.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(interface, "pipeline_capability", _available)

    def processor(*_args, **_kwargs):
        return {
            "pages": [{"page": 1, "width": 1000, "height": 800}],
            "diagnostics": {
                "ocr_used": False,
                "semantic_interpretation": "hermes_agent_event_loop",
            },
        }

    result = interface.execute_2d_pipeline(str(source), processor=processor)

    assert result.pages == [DrawingPage(page=1, width=1000.0, height=800.0)]
    assert result.diagnostics["ocr_used"] is False
    serialized = json.dumps(result.to_dict())
    assert "fragment" not in serialized.lower()
    assert "raw_text" not in serialized.lower()


def test_real_pipeline_renders_and_crops_in_memory_without_ocr_artifacts(tmp_path):
    fitz = pytest.importorskip("fitz")
    source = tmp_path / "drawing.pdf"
    document = fitz.open()
    page = document.new_page(width=1000, height=800)
    page.insert_textbox(
        fitz.Rect(80, 80, 700, 220),
        "NOTES:\n1. MATERIAL: ABS\n2. WALL 1.2 mm",
        fontsize=18,
    )
    document.save(source)
    document.close()

    result = interface.execute_2d_pipeline(str(source))
    overviews = interface.render_overviews(str(source))
    crops = interface.prepare_crops(
        str(source),
        [
            {
                "page": 1,
                "region_id": "notes-main",
                "type": "notes",
                "bbox_1000": [60, 60, 720, 250],
                "decision": "keep",
                "reason": "Complete numbered notes block",
            }
        ],
    )

    assert result.pages == [DrawingPage(page=1, width=1000.0, height=800.0)]
    assert result.diagnostics["ocr_used"] is False
    assert len(overviews) == 1 and overviews[0].png_bytes.startswith(b"\x89PNG")
    assert len(crops) == 1 and crops[0].png_bytes.startswith(b"\x89PNG")
    assert crops[0].region_id == "notes-main"
    assert not list(tmp_path.glob("*.png"))
    assert not list(tmp_path.glob("*.jsonl"))


def test_drawing_analyzer_emits_visual_diagnostics_without_image_artifacts(tmp_path):
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
    analyzer = _drawing_analyzer()

    artifacts = analyzer.run(context, CancellationToken())

    assert {item.kind for item in artifacts} == {"drawing_diagnostics"}
    diagnostic = json.loads(
        (tmp_path / artifacts[0].relative_path).read_text(encoding="utf-8")
    )
    assert diagnostic["input_id"] == record.input_id
    assert diagnostic["page_count"] == 1
    assert diagnostic["ocr_used"] is False
    assert not list((tmp_path / "discovery").rglob("*.png"))
    assert all(WorkerEvent.from_dict(item.to_dict()) for item in events)
    assert events[-1].type == "completed"


def test_drawing_pipeline_errors_are_not_materialized_as_success(tmp_path):
    record = _input()
    source = tmp_path / record.relative_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    events = []

    def failing(*_args, **_kwargs):
        raise DrawingPipelineError("drawing_render_failed", "Rendering failed")

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

    assert exc_info.value.code == "drawing_render_failed"
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
            "source_type": "DWG",
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
    registry.register(_drawing_analyzer())
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    service = DFMService(
        config=DFMConfig(geometry_backend="occt_cpp_external"),
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
        assert (
            service._objective_analyzer_key(manifest, "pythonocc_internal")
            == "step"
        )
        with pytest.raises(DFMError) as exc_info:
            registry.get(analyzer_key)
        assert exc_info.value.code == "analyzer_not_found"
    finally:
        service.close()


def test_mixed_input_uses_agent_observation_and_fusion_submission_flow(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(_drawing_analyzer())
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    service = DFMService(
        config=DFMConfig(geometry_backend="pythonocc_internal"),
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
        assert drawing_context["_multimodal"] is True
        assert drawing_context["meta"]["page_count"] == 1
        assert any(item["type"] == "image_url" for item in drawing_context["content"])
        crop_context = service.analysis(
            "submit_crop_plan",
            project_id=project_id,
            input_id=drawing_input["input_id"],
            expected_revision=drawing_context["meta"]["expected_revision"],
            crop_regions=[
                {
                    "page": 1,
                    "region_id": "region.material",
                    "type": "materials_bom",
                    "bbox_1000": [10, 20, 300, 180],
                    "decision": "keep",
                },
                {
                    "page": 1,
                    "region_id": "region.wall",
                    "type": "manufacturing_callouts",
                    "bbox_1000": [350, 200, 700, 400],
                    "decision": "keep",
                },
            ],
        )
        assert crop_context["_multimodal"] is True
        assert crop_context["meta"]["available_region_ids"] == [
            "region.material",
            "region.wall",
        ]
        submitted = service.analysis(
            "submit_observations",
            project_id=project_id,
            input_id=drawing_input["input_id"],
            expected_revision=crop_context["meta"]["expected_revision"],
            observations=[
                {
                    "kind": "material",
                    "value": "ABS",
                    "confidence": 0.95,
                    "source_region_refs": ["region.material"],
                    "source_text": "MATERIAL ABS",
                },
                {
                    "kind": "wall_thickness",
                    "value": 1.2,
                    "unit": "mm",
                    "confidence": 0.9,
                    "source_region_refs": ["region.wall"],
                    "source_text": "WALL 1.2 mm",
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
        manifest = service._store(project_id).load()
        discovery_plan = next(item for item in manifest.plans if item.phase == "discovery")
        operations = {
            item.operation_id: item for item in discovery_plan.operations
        }
        assert operations["discovery.generic_geometry"].depends_on == []
        assert set(operations["discovery.fusion"].depends_on) == {
            "discovery.process_features",
            "discovery.agent_interpretation",
        }
    finally:
        service.close()


def test_discover_runs_two_drawing_vision_passes_with_main_runtime(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(_drawing_analyzer())
    registry.register(FusionAnalyzer())
    calls = []

    def vision_call(*, content, runtime, max_tokens):
        calls.append((content, runtime, max_tokens))
        if len(calls) == 1:
            return json.dumps({
                "regions": [{
                    "page": 1,
                    "region_id": "p1_notes",
                    "type": "notes",
                    "bbox_1000": [10, 20, 700, 400],
                    "decision": "keep",
                    "reason": "Complete notes block",
                }]
            })
        return json.dumps({
            "observations": [{
                "kind": "global_note",
                "value": "1. MATERIAL: ABS",
                "confidence": 0.95,
                "source_region_refs": ["p1_notes"],
                "source_text": "1. MATERIAL: ABS",
            }]
        })

    service = DFMService(
        workspace=DFMWorkspace(tmp_path / "workspace"),
        registry=registry,
        reconcile_jobs=False,
        vision_call=vision_call,
    )
    try:
        project_id = service.project("create", name="Background vision")["project_id"]
        drawing_path = tmp_path / "drawing.png"
        drawing_path.write_bytes(b"png")
        drawing = service.project(
            "add_input", project_id=project_id, path=str(drawing_path)
        )["input"]

        result = service.analysis(
            "discover",
            project_id=project_id,
            _main_runtime={"provider": "custom:test", "model": "main-model"},
        )
        manifest = service._store(project_id).load()
        state = manifest.capabilities["drawing_discovery"]["inputs"][drawing["input_id"]]

        assert len(calls) == 2
        assert all(call[1]["model"] == "main-model" for call in calls)
        assert result.get("status") != "agent_interpretation_required"
        assert state["interpretation_status"] == "completed"
        assert state["region_count"] == 1
        assert [item.kind for item in manifest.observations] == ["global_note"]
    finally:
        service.close()


def test_drawing_vision_and_geometry_discovery_overlap_and_reconcile_conflict(
    tmp_path, monkeypatch
):
    registry = AnalyzerRegistry()
    registry.register(_drawing_analyzer())
    registry.register(FusionAnalyzer())
    service = DFMService(
        workspace=DFMWorkspace(tmp_path / "workspace"),
        registry=registry,
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="Parallel discovery")["project_id"]
        rendezvous = threading.Barrier(2)
        worker_names = set()
        geometry_calls = 0

        def geometry_branch(selected_project_id):
            nonlocal geometry_calls
            geometry_calls += 1
            if geometry_calls == 1:
                worker_names.add(threading.current_thread().name)
                rendezvous.wait(timeout=2)
                raise DFMError("manifest_conflict", "Concurrent drawing update")
            return service._store(selected_project_id).load()

        def drawing_branch(selected_project_id, input_id, runtime):
            assert input_id == "input_drawing"
            assert runtime["model"] == "main-model"
            worker_names.add(threading.current_thread().name)
            rendezvous.wait(timeout=2)

        monkeypatch.setattr(service, "_persist_geometry_candidates", geometry_branch)
        monkeypatch.setattr(
            service, "_interpret_drawing_in_background", drawing_branch
        )

        manifest = service._discover_geometry_and_drawings_in_parallel(
            project_id,
            ["input_drawing"],
            {"provider": "custom:test", "model": "main-model"},
        )

        assert manifest.project_id == project_id
        assert len(worker_names) == 2
        assert all(name.startswith("dfm-discovery") for name in worker_names)
        assert geometry_calls == 2
    finally:
        service.close()


def test_agent_observation_rejects_evidence_from_outside_context(tmp_path):
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(_drawing_analyzer())
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
        crop_context = service.analysis(
            "submit_crop_plan",
            project_id=project_id,
            input_id=drawing["input_id"],
            expected_revision=context["meta"]["expected_revision"],
            crop_regions=[
                {
                    "page": 1,
                    "region_id": "region.valid",
                    "type": "materials_bom",
                    "bbox_1000": [10, 20, 300, 180],
                    "decision": "keep",
                }
            ],
        )

        with pytest.raises(DFMError) as exc_info:
            service.analysis(
                "submit_observations",
                project_id=project_id,
                input_id=drawing["input_id"],
                expected_revision=crop_context["meta"]["expected_revision"],
                observations=[
                    {
                        "kind": "material",
                        "value": "ABS",
                        "confidence": 0.9,
                        "source_region_refs": ["region.fabricated"],
                    }
                ],
            )

        assert exc_info.value.code == "observation_evidence_invalid"
    finally:
        service.close()
