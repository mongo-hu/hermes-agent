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


SCHEMA_ROOT = Path(__file__).parents[3] / "tools" / "dfm" / "schemas"


def _validate(name: str, payload: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


def test_feature_observation_and_fusion_link_match_formal_schemas():
    feature = FeatureRecord(
        feature_id="feature.screw_boss.003",
        kind="screw_boss",
        source_refs=["input:model"],
        confidence=0.96,
        input_sha256="a" * 64,
        region_refs=["region.screw_boss.003.outer_wall"],
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
