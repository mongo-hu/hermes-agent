import json
from pathlib import Path

import pytest

from tools.dfm.contracts import (
    DiscoverySnapshotRecord,
    FeatureRecord,
    FusionLinkRecord,
    ObservationRecord,
    RuleBinding,
)
from tools.dfm.discovery import _normalize_coverage
from tools.dfm.errors import DFMError


SCHEMA_ROOT = Path(__file__).parents[3] / "tools" / "dfm" / "schemas"


def _validate(name: str, payload: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_feature_observation_and_fusion_link_match_formal_schemas():
    feature = FeatureRecord(
        feature_id="feature.hollow_boss.003",
        kind="hollow_boss",
        source_refs=["input:model"],
        confidence=0.96,
        input_sha256="a" * 64,
        region_refs=["region.hollow_boss.003.outer_wall"],
        properties={"height_mm": 12.5, "thickness_mm": 1.8},
        relationships=[
            {
                "relation": "attached_to",
                "target_ref": "feature.main_wall.001",
                "confidence": 0.9,
            }
        ],
        recognizer="occt_cpp_molding_feature_recognizer",
        recognizer_version="1.0.0",
    )
    observation = ObservationRecord(
        observation_id="observation.drawing.17",
        input_id="input_drawing",
        kind="minimum_draft_angle",
        value=1.0,
        source_refs=["drawing:page=2:bbox=120,80,180,110"],
        confidence=0.91,
        unit="degree",
        provenance={"provider": "drawing-placeholder", "version": "0"},
    )
    link = FusionLinkRecord(
        fusion_link_id="fusion.17",
        observation_refs=[observation.observation_id],
        feature_refs=[feature.feature_id],
        region_refs=feature.region_refs,
        confidence=0.88,
        status="confirmed",
        method="drawing_callout_to_feature_region",
    )
    snapshot = DiscoverySnapshotRecord(
        snapshot_id="discovery.snapshot.1",
        created_at="2026-08-11T10:00:30Z",
        input_hashes={"input_model": "a" * 64},
        observation_refs=[observation.observation_id],
        feature_refs=[feature.feature_id],
        region_refs=feature.region_refs,
        fusion_link_refs=[link.fusion_link_id],
        provider_versions={"occt_cpp_molding_feature_recognizer": "1.0.0"},
        content_sha256="b" * 64,
        geometry_snapshot_ref="artifact.geometry_snapshot",
        topology_snapshot_id="topology.snapshot.1",
        render_mesh_snapshot_id="render.snapshot.1",
        artifact_refs=[
            "artifact.geometry_snapshot",
            "artifact.topology_map",
            "artifact.render_scene",
        ],
    )

    _validate("feature.schema.json", feature.to_dict())
    _validate("observation.schema.json", observation.to_dict())
    _validate("fusion_link.schema.json", link.to_dict())
    _validate("discovery_snapshot.schema.json", snapshot.to_dict())


def test_rule_binding_fact_dependencies_match_formal_schema():
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

    _validate("rule_binding.schema.json", binding.to_dict())


def _coverage_row(**overrides):
    return {
        "feature_kind": "forming_plan",
        "context": {},
        "status": "partial",
        "covered_geometry_ref": {"face_ids": ["face:1"]},
        "unresolved_geometry_ref": None,
        "diagnostics": {"requires_user_input": True},
        **overrides,
    }


def _coverage_snapshot_payload(coverage):
    snapshot = DiscoverySnapshotRecord(
        snapshot_id="discovery.scoped", created_at="2026-11-03T00:00:00Z",
        input_hashes={"input_model": "a" * 64}, observation_refs=[], feature_refs=[],
        region_refs=[], fusion_link_refs=[], provider_versions={"worker": "test"},
        content_sha256="b" * 64, coverage=coverage,
    )
    return json.loads(json.dumps(snapshot.to_dict()))


def test_coverage_preserves_distinct_forming_scopes_through_snapshot_roundtrip():
    # MAIN analysis emits one partial forming-plan row per local scope, all with context={}.
    raw = [_coverage_row(forming_scope_id="undercut_left"),
           _coverage_row(forming_scope_id="undercut_right")]
    normalized = _normalize_coverage(raw)
    assert normalized == raw
    payload = _coverage_snapshot_payload(normalized)
    _validate("discovery_snapshot.schema.json", payload)
    assert DiscoverySnapshotRecord.from_dict(payload).coverage == raw


@pytest.mark.parametrize("normalize", [False, True])
@pytest.mark.parametrize("references", [
    {},
    {"covered_geometry_ref": None, "unresolved_geometry_ref": None},
    {"covered_geometry_ref": {"body_ids": ["body:1"]},
     "unresolved_geometry_ref": {"face_ids": ["face:1"]}},
])
def test_session_coverage_without_face_references_roundtrips(references, normalize):
    # Session-v2 status rows may omit geometry references; the normalizer emits null.
    row = _coverage_row(forming_scope_id="SIDE")
    row.pop("covered_geometry_ref")
    row.pop("unresolved_geometry_ref")
    row.update(references)
    coverage = _normalize_coverage([row]) if normalize else [row]
    payload = _coverage_snapshot_payload(coverage)
    _validate("discovery_snapshot.schema.json", payload)
    assert DiscoverySnapshotRecord.from_dict(payload).coverage == coverage


@pytest.mark.parametrize("reference", ["covered_geometry_ref", "unresolved_geometry_ref"])
def test_coverage_snapshot_rejects_non_object_geometry_references(reference):
    jsonschema = pytest.importorskip("jsonschema")
    payload = _coverage_snapshot_payload([_coverage_row(**{reference: "not-a-reference"})])
    with pytest.raises(jsonschema.ValidationError):
        _validate("discovery_snapshot.schema.json", payload)


@pytest.mark.parametrize("scope_fields", [{}, {"forming_scope_id": None}, {"forming_scope_id": ""}])
def test_coverage_retains_optional_scope_representation(scope_fields):
    raw = _coverage_row(**scope_fields)
    assert _normalize_coverage([raw]) == [raw]


@pytest.mark.parametrize("left_scope,right_scope", [
    ({}, {}), ({}, {"forming_scope_id": None}),
    ({"forming_scope_id": None}, {}),
    ({"forming_scope_id": None}, {"forming_scope_id": None}),
    ({"forming_scope_id": "SIDE"}, {"forming_scope_id": "SIDE"}),
])
def test_coverage_rejects_same_scope_and_context_regardless_of_key_order(left_scope, right_scope):
    rows = [
        _coverage_row(context={"direction": [0, 0, 1], "options": {"a": 1, "b": 2}}, **left_scope),
        _coverage_row(context={"options": {"b": 2, "a": 1}, "direction": [0, 0, 1]}, **right_scope),
    ]
    with pytest.raises(DFMError, match="duplicate coverage identities") as caught:
        _normalize_coverage(rows)
    assert caught.value.code == "geometry_protocol_invalid"


def test_coverage_distinguishes_kind_context_and_explicit_empty_scope():
    rows = [
        _coverage_row(),
        _coverage_row(forming_scope_id=""),
        _coverage_row(forming_scope_id="SIDE"),
        _coverage_row(forming_scope_id="SIDE", context={"direction": [0, 0, 1]}),
        _coverage_row(forming_scope_id="SIDE", feature_kind="forming_scope"),
    ]
    assert _normalize_coverage(rows) == rows


@pytest.mark.parametrize("scope_id", [0, True, [], {}])
def test_coverage_rejects_non_string_non_null_scope(scope_id):
    with pytest.raises(DFMError, match="invalid coverage row") as caught:
        _normalize_coverage([_coverage_row(forming_scope_id=scope_id)])
    assert caught.value.code == "geometry_protocol_invalid"
