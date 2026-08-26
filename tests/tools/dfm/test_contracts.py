import json

import pytest

from tools.dfm.contracts import (
    MANIFEST_SCHEMA_VERSION,
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    ClarificationRecord,
    DiscoverySnapshotRecord,
    FactRecord,
    FeatureRecord,
    FusionLinkRecord,
    FindingRecord,
    InputRecord,
    MeasurementRecord,
    ObservationRecord,
    PlanOperation,
    PlanRecord,
    ProjectManifest,
    ResolvedArgument,
    RuleBinding,
    RunRecord,
    RunStatus,
    ensure_run_transition,
)
from tools.dfm.errors import DFMError


def test_contract_state_values_are_stable():
    assert {item.value for item in CapabilityStatus} == {
        "available",
        "dependency_missing",
        "not_implemented",
        "blocked",
        "disabled",
        "unhealthy",
    }
    assert {item.value for item in RunStatus} == {
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "blocked",
    }


def test_manifest_contract_serializes_to_json_compatible_dict():
    manifest = ProjectManifest(
        project_id="dfm_123",
        name="Bracket",
        created_at="2026-07-13T10:00:00Z",
        updated_at="2026-07-13T10:01:00Z",
        inputs=[
            InputRecord(
                input_id="input_1",
                kind="step",
                source_name="bracket.step",
                relative_path="inputs/bracket.step",
                size_bytes=12,
                sha256="a" * 64,
                created_at="2026-07-13T10:00:10Z",
            )
        ],
        runs=[
            RunRecord(
                run_id="run_1",
                analyzer_key="step",
                analyzer_version="unimplemented",
                status=RunStatus.SUCCEEDED,
                created_at="2026-07-13T10:00:20Z",
                updated_at="2026-07-13T10:01:00Z",
                artifacts=[
                    ArtifactRecord(
                        artifact_id="artifact_1",
                        kind="diagnostic",
                        relative_path="artifacts/run_1/status.json",
                        media_type="application/json",
                        created_at="2026-07-13T10:01:00Z",
                    )
                ],
            )
        ],
    )

    payload = manifest.to_dict()

    assert payload["schema_version"] == MANIFEST_SCHEMA_VERSION == 1
    assert payload["runs"][0]["status"] == "succeeded"
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.QUEUED, RunStatus.CANCELLED),
        (RunStatus.QUEUED, RunStatus.BLOCKED),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.CANCELLED),
    ],
)
def test_valid_run_transitions_are_accepted(current, target):
    ensure_run_transition(current, target)


def test_terminal_run_transition_is_rejected():
    with pytest.raises(DFMError) as exc_info:
        ensure_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)

    assert exc_info.value.code == "invalid_run_transition"


def test_capability_and_error_envelopes_are_stable():
    capability = Capability(
        analyzer_key="drawing",
        status=CapabilityStatus.NOT_IMPLEMENTED,
        reason="Drawing analysis is planned for a later milestone.",
        error_code="unsupported_capability",
    )
    error = DFMError(
        "unsupported_capability",
        "Drawing analysis is not implemented.",
        {"analyzer_key": "drawing"},
    )

    assert capability.to_dict() == {
        "analyzer_key": "drawing",
        "status": "not_implemented",
        "reason": "Drawing analysis is planned for a later milestone.",
        "error_code": "unsupported_capability",
        "details": {},
    }
    assert error.to_dict() == {
        "ok": False,
        "error": {
            "code": "unsupported_capability",
            "message": "Drawing analysis is not implemented.",
            "details": {"analyzer_key": "drawing"},
        },
    }


def test_manifest_carries_m0_workflow_records():
    manifest = ProjectManifest(
        project_id="dfm_123",
        name="Bracket",
        created_at="2026-07-13T10:00:00Z",
        updated_at="2026-07-13T10:01:00Z",
        domain="injection_molding",
        input_mode="step",
        facts=[FactRecord("fact_1", "material", "ABS", "user", "confirmed")],
        clarifications=[
            ClarificationRecord("clar_1", "Which material?", "answered", "ABS")
        ],
        features=[FeatureRecord("feature_1", "hole", ["input_1"], 0.9)],
        plans=[PlanRecord("plan_1", "step", ["step"], "ready", "2026-07-13T10:00:30Z")],
        findings=[
            FindingRecord(
                finding_id="finding_1",
                title="Thin wall",
                severity="high",
                status="open",
                evaluation_ids=["evaluation_1"],
                measurement_ids=["measurement_1"],
                metric_ids=["injection.geometry.wall_thickness"],
                region_refs=[],
                evidence_refs=["artifact_1"],
                rule_refs=["rule:wall-thickness:1"],
                recommendation="Increase local thickness.",
            )
        ],
    )

    restored = ProjectManifest.from_dict(manifest.to_dict())

    assert restored == manifest
    assert restored.revision == 0
    assert restored.facts[0].status == "confirmed"


def test_plan_operation_round_trips_as_the_only_production_shape():
    operation = PlanOperation(
        operation_id="draft.fixed_half",
        calculator_id="measure_draft",
        depends_on=["geometry.topology"],
        metric_ids=["dc.geometry.draft.fixed_half"],
        required_quantities=["draft_angle_deg"],
        arguments={
            "pull_direction": ResolvedArgument(
                [0, 0, 1], "fact:pull_direction.fixed_half"
            ),
            "region": ResolvedArgument(
                {"region_id": "region.fixed_half", "mode": "bbox"},
                "region:region.fixed_half@1",
            ),
        },
    )

    payload = operation.to_dict()
    assert payload["calculator_id"] == "measure_draft"
    assert "operation" not in payload
    assert PlanOperation.from_dict(payload) == operation


def test_measurement_references_plan_operation_metric_and_calculator():
    measurement = MeasurementRecord(
        measurement_id="measurement_draft_fixed_half_min",
        operation_id="draft.fixed_half",
        calculator_id="measure_draft",
        metric_id="dc.geometry.draft.fixed_half",
        quantity_id="draft_angle_deg",
        value=1.2,
        unit="degree",
        status="measured",
        geometry_refs=[],
        method="nx_open_draft_analysis",
        algorithm_version="nx-draft-1",
        input_sha256="a" * 64,
    )

    assert MeasurementRecord.from_dict(measurement.to_dict()) == measurement


def test_rule_binding_separates_rule_facts_from_geometry_facts():
    binding = RuleBinding(
        binding_id="binding.wall.minimum",
        operation_id="geometry.wall_thickness",
        metric_id="injection.geometry.wall_thickness",
        quantity_id="thickness_mm",
        rule_id="min_wall_mm",
        operator=">=",
        aggregation="minimum",
        required_fact_names=["material"],
        feature_refs=["feature.ordinary.1"],
        region_refs=["region.ordinary.1"],
    )

    payload = binding.to_dict()
    assert RuleBinding.from_dict(payload) == binding
    assert payload["required_fact_names"] == ["material"]


def test_discovery_contract_links_observations_features_regions_and_analysis_plan():
    feature = FeatureRecord(
        "feature.screw_boss.003",
        "screw_boss",
        ["input:model"],
        0.96,
        input_sha256="a" * 64,
        region_refs=["region.screw_boss.003.outer_wall"],
        properties={"outer_radius_mm": 4.0, "inner_radius_mm": 2.2},
        recognizer="occt_cpp_molding_feature_recognizer",
        recognizer_version="1.0.0",
    )
    observation = ObservationRecord(
        "observation.drawing.17",
        "input_drawing",
        "minimum_draft_angle",
        1.0,
        ["drawing:page=2:bbox=120,80,180,110"],
        0.91,
        unit="degree",
    )
    fusion = FusionLinkRecord(
        "fusion.17",
        [observation.observation_id],
        [feature.feature_id],
        feature.region_refs,
        0.88,
        "confirmed",
        "drawing_callout_to_feature_region",
    )
    snapshot = DiscoverySnapshotRecord(
        "discovery.snapshot.1",
        "2026-08-11T10:00:30Z",
        {"input_model": "a" * 64},
        [observation.observation_id],
        [feature.feature_id],
        feature.region_refs,
        [fusion.fusion_link_id],
        {"occt_cpp_molding_feature_recognizer": "1.0.0"},
        "b" * 64,
    )
    discovery = PlanRecord(
        "plan.discovery.1",
        "fusion",
        ["drawing", "step"],
        "ready",
        "2026-08-11T10:00:00Z",
        phase="discovery",
    )
    analysis = PlanRecord(
        "plan.analysis.1",
        "fusion",
        ["step"],
        "ready",
        "2026-08-11T10:01:00Z",
        phase="analysis",
        parent_plan_id=discovery.plan_id,
        discovery_snapshot_refs=[
            "feature-set:run.discovery.1",
            "region-set:run.discovery.1",
            "fusion-links:revision.1",
        ],
    )

    manifest = ProjectManifest(
        project_id="dfm_123",
        name="Feature-aware part",
        created_at="2026-08-11T10:00:00Z",
        updated_at="2026-08-11T10:01:00Z",
        features=[feature],
        observations=[observation],
        fusion_links=[fusion],
        discovery_snapshots=[snapshot],
        plans=[discovery, analysis],
    )

    assert ProjectManifest.from_dict(manifest.to_dict()) == manifest
    assert analysis.phase == "analysis"
    assert analysis.discovery_snapshot_refs[0].startswith("feature-set:")
