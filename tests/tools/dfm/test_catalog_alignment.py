"""Published Mold catalog -> SQLite -> pinned Plan -> Measurement evaluation."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from tools.dfm.contracts import (
    FeatureRecord,
    RegionRecord,
    PlanOperation,
    PlanRecord,
    MeasurementRecord,
)
from tools.dfm.discovery import DiscoveryEngine
from tools.dfm.errors import DFMError
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.ontology import LocalOntologyStore


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "tools/dfm/scopes/injection/ontology_snapshot_v2.json"
METRIC = "injection.geometry.wall_thickness"


def payload():
    value = json.loads(PACKAGE.read_text(encoding="utf-8"))
    value["rules"] = [
        row
        for row in value["rules"]
        if row["check_id"] == "check.main_wall_minimum_thickness"
    ]
    return value


def rehash(value):
    value.pop("content_sha256", None)
    value["content_sha256"] = hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return value


def operations():
    return [
        PlanOperation(
            "wall",
            "measure_wall_thickness",
            metric_ids=[METRIC],
            required_quantities=["thickness_mm"],
        )
    ]


def conditional_payload():
    value = payload()
    rule = value["rules"][0]
    rule["conditions"].append({
        "geometric_id": "actual",
        "operator": "LT",
        "value": 5,
        "unit": "mm",
    })
    default = deepcopy(rule)
    default.update(
        rule_id="R_FALLBACK",
        rule_version_id="fallback.version",
        conditions=[],
        is_default=True,
        priority=0,
        threshold=0.7,
    )
    value["rules"].append(default)
    return rehash(value)


def plan(compiled):
    return PlanRecord(
        plan_id="catalog.plan",
        input_mode="step",
        analyzer_keys=["step"],
        status="ready",
        created_at="2026-09-17T00:00:00Z",
        process="injection",
        scope_id=compiled.identity.scope_id,
        scope_version=compiled.identity.scope_version,
        rules=compiled.rules,
        rule_bindings=compiled.rule_bindings,
        operations=operations(),
        ontology_snapshot_id=compiled.identity.snapshot_id,
        ontology_snapshot_sha256=compiled.identity.content_sha256,
    )


def measurement(value, unit="mm"):
    return MeasurementRecord(
        "m.wall",
        "wall",
        "measure_wall_thickness",
        METRIC,
        "thickness_mm",
        value,
        unit,
        "measured",
        [],
        "test",
        "1",
        "a" * 64,
    )


def test_mold_and_agent_publish_the_same_schema_when_both_checkouts_exist():
    mold = ROOT.parent / "Mold/backend/aimold_app/schemas/ontology_snapshot.schema.json"
    if not mold.is_file():
        pytest.skip("Mold checkout is not available in this environment")
    assert json.loads(mold.read_text(encoding="utf-8")) == json.loads(
        (ROOT / "tools/dfm/schemas/ontology_snapshot.schema.json").read_text(
            encoding="utf-8"
        )
    )


def test_current_package_installs_without_ontology_region_and_preserves_raw_text(
    tmp_path,
):
    value = rehash(payload())
    store = LocalOntologyStore(tmp_path / "catalog.sqlite3")
    store.install_package(value)
    context = store.check_context(value["rules"][0]["check_id"])
    operand = next(
        row for row in context["relations"] if row["predicate"] == "USES_OPERAND"
    )
    assert operand["object"]["concept_type"] == "geometric"
    assert operand["qualifiers"]["operand_text"] == next(
        row["qualifiers"]["operand_text"]
        for row in value["relations"]
        if row["predicate"] == "USES_OPERAND"
    )
    assert all(
        row["predicate"] not in {"APPLIES_TO_REGION", "HAS_REGION"}
        for row in context["relations"]
    )
    assert store.compile("injection", {"material": "ABS"}, operations()).rule_bindings


def test_bundled_package_content_hash_is_valid_without_rewriting():
    store = LocalOntologyStore.from_package(PACKAGE)
    assert (
        store.identity().content_sha256
        == json.loads(PACKAGE.read_text(encoding="utf-8"))["content_sha256"]
    )


@pytest.mark.parametrize(
    "change",
    ["concept_id", "unit", "nan", "mixed", "blank_text", "region_cache", "old_binding"],
)
def test_invalid_current_catalog_cannot_replace_installed_snapshot(tmp_path, change):
    value = conditional_payload()
    store = LocalOntologyStore(tmp_path / "catalog.sqlite3")
    store.install_package(value)
    original = store.identity()
    condition = value["rules"][0]["conditions"][1]
    operand = next(
        row for row in value["relations"] if row["predicate"] == "USES_OPERAND"
    )
    geometric = next(
        row for row in value["concepts"] if row["concept_id"] == operand["object_id"]
    )
    if change == "concept_id":
        condition["geometric_id"] = operand["object_id"]
    if change == "unit":
        condition["unit"] = "cm"
    if change == "nan":
        condition["value"] = float("nan")
    if change == "mixed":
        condition["factor_id"] = "factor.material"
    if change == "blank_text":
        operand["qualifiers"]["operand_text"] = "  "
    if change == "region_cache":
        geometric["properties"]["worker_role"] = "wall"
    if change == "old_binding":
        geometric["properties"]["worker_metric_id"] = geometric["properties"].pop(
            "worker_geometric_id"
        )
    with pytest.raises(DFMError):
        store.install_package(rehash(value))
    assert store.identity() == original


@pytest.mark.parametrize(
    "value,winner",
    [(1.0, "R_INJ_MAIN_WALL_MIN_ABS"), (5.0, "R_FALLBACK"), (8.0, "R_FALLBACK")],
)
def test_geometry_selects_one_rule_after_measurement_and_plan_roundtrip(value, winner):
    store = LocalOntologyStore.from_package(conditional_payload())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    assert {row.rule_id for row in compiled.rule_bindings} == {
        "R_INJ_MAIN_WALL_MIN_ABS",
        "R_FALLBACK",
    }
    pinned = PlanRecord.from_dict(plan(compiled).to_dict())
    evaluations, provenance = EvaluationEngine().evaluate([measurement(value)], pinned)
    assert [row.rule_id for row in evaluations] == [winner]
    assert evaluations[0].operand_values["actual"]["measurement_ids"] == ["m.wall"]
    assert provenance[evaluations[0].evaluation_id]["rule_selection"] is not None


def test_missing_or_wrong_unit_measurement_does_not_silently_select_default():
    compiled = LocalOntologyStore.from_package(conditional_payload()).compile(
        "injection", {"material": "ABS"}, operations()
    )
    for rows in ([], [measurement(2, "cm")]):
        with pytest.raises(DFMError):
            EvaluationEngine().evaluate(rows, plan(compiled))


def test_conflicting_measured_candidates_are_reported():
    value = conditional_payload()
    conflict = deepcopy(value["rules"][0])
    conflict.update(
        rule_id="R_CONFLICT", rule_version_id="conflict.version", threshold=2.5
    )
    value["rules"].append(conflict)
    compiled = LocalOntologyStore.from_package(rehash(value)).compile(
        "injection", {"material": "ABS"}, operations()
    )
    with pytest.raises(DFMError) as error:
        EvaluationEngine().evaluate([measurement(2)], plan(compiled))
    assert error.value.code == "ontology_rule_conflict"


def test_false_factor_condition_eliminates_candidate_without_geometry_lookup():
    compiled = LocalOntologyStore.from_package(conditional_payload()).compile(
        "injection", {"material": "PC"}, operations()
    )
    assert [row.rule_id for row in compiled.rule_bindings] == ["R_FALLBACK"]


def test_current_rule_binding_matches_its_wire_schema():
    compiled = LocalOntologyStore.from_package(conditional_payload()).compile(
        "injection", {"material": "ABS"}, operations()
    )
    schema = json.loads(
        (ROOT / "tools/dfm/schemas/rule_binding.schema.json").read_text(
            encoding="utf-8"
        )
    )
    for binding in compiled.rule_bindings:
        Draft202012Validator(schema).validate(binding.to_dict())


def test_condition_only_operand_does_not_replace_expression_anchor():
    value = conditional_payload()
    geometric = deepcopy(
        next(item for item in value["concepts"] if item["concept_type"] == "geometric")
    )
    geometric["concept_id"] = "G_GATE"
    geometric["properties"]["worker_geometric_id"] = "metric.gate"
    value["concepts"].append(geometric)
    relation = deepcopy(
        next(item for item in value["relations"] if item["predicate"] == "USES_OPERAND")
    )
    relation.update(relation_id="relation.gate", object_id="G_GATE", sort_order=-1)
    relation["qualifiers"]["alias"] = "gate"
    value["relations"].append(relation)
    value["rules"][0]["conditions"][1]["geometric_id"] = "gate"
    ops = [
        *operations(),
        PlanOperation(
            "gate",
            "measure_gate",
            metric_ids=["metric.gate"],
            required_quantities=["thickness_mm"],
        ),
    ]
    compiled = LocalOntologyStore.from_package(rehash(value)).compile(
        "injection", {"material": "ABS"}, ops
    )
    assert all(binding.operand_alias == "actual" for binding in compiled.rule_bindings)
    pinned = replace(plan(compiled), operations=ops)
    pinned = PlanRecord.from_dict(pinned.to_dict())
    gate = replace(
        measurement(3),
        measurement_id="m.gate",
        operation_id="gate",
        metric_id="metric.gate",
    )
    evaluations, _ = EvaluationEngine().evaluate([measurement(1), gate], pinned)
    assert [item.rule_id for item in evaluations] == [value["rules"][0]["rule_id"]]
    assert set(evaluations[0].measurement_ids) == {"m.wall", "m.gate"}


def test_measured_rule_selection_is_per_feature_instance():
    compiled = LocalOntologyStore.from_package(conditional_payload()).compile(
        "injection", {"material": "ABS"}, operations()
    )
    bindings, measurements = [], []
    for feature, value in (("feature.a", 1), ("feature.b", 8)):
        bindings.extend(
            replace(
                binding, binding_id=binding.binding_id + feature, feature_refs=[feature]
            )
            for binding in compiled.rule_bindings
        )
        measurements.append(
            replace(
                measurement(value),
                measurement_id=feature + ".measurement",
                feature_refs=[feature],
            )
        )
    evaluations, _ = EvaluationEngine().evaluate(
        measurements, replace(plan(compiled), rule_bindings=bindings)
    )
    assert {item.feature_refs[0]: item.rule_id for item in evaluations} == {
        "feature.a": "R_INJ_MAIN_WALL_MIN_ABS",
        "feature.b": "R_FALLBACK",
    }


def test_legacy_binding_serialization_does_not_change_historical_rule_hash_input():
    old = ROOT / "tests/tools/dfm/fixtures/ontology_legacy_v2.json"
    ops = [
        *operations(),
        PlanOperation(
            "draft",
            "measure_draft",
            metric_ids=["injection.geometry.draft"],
            required_quantities=["draft_angle_deg"],
        ),
    ]
    compiled = LocalOntologyStore.from_package(old).compile(
        "injection", {"material": "ABS"}, ops
    )
    assert all(
        "rule_selection" not in binding.to_dict() for binding in compiled.rule_bindings
    )


def discovered(kind="ordinary_part", role="ordinary"):
    feature = FeatureRecord(
        "feature.1",
        kind,
        ["input.1"],
        1.0,
        input_sha256="a" * 64,
        region_refs=["region.1"],
    )
    region = RegionRecord(
        "region.1",
        "a" * 64,
        "model",
        "whole_model",
        "actual region",
        ["input.1"],
        "1",
        "b" * 64,
        role=role,
        feature_refs=[feature.feature_id],
    )
    manifest = SimpleNamespace(
        features=[feature], regions=[region], process="injection"
    )
    snapshot = SimpleNamespace(
        feature_refs=[feature.feature_id], region_refs=[region.region_id]
    )
    return manifest, snapshot


def test_discovery_uses_comparison_text_without_region_concepts_and_reuses_target():
    store = LocalOntologyStore.from_package(conditional_payload())
    manifest, snapshot = discovered()
    targets = DiscoveryEngine(ontology_store=store).analysis_targets(manifest, snapshot)
    assert len([item for item in targets if item["metric_id"] == METRIC]) == 1
    assert all(item["region"] is manifest.regions[0] for item in targets)
    assert all(item["operand_anchors"] for item in targets)


def test_unrecognized_comparison_text_is_not_treated_as_whole_part():
    value = payload()
    for row in value["relations"]:
        if row["predicate"] == "USES_OPERAND":
            row["qualifiers"]["operand_text"] = "an unspecified custom local patch"
    engine = DiscoveryEngine(
        ontology_store=LocalOntologyStore.from_package(rehash(value))
    )
    with pytest.raises(DFMError) as error:
        engine.analysis_targets(*discovered())
    assert error.value.details["operand_text"] == "an unspecified custom local patch"


def test_adjacent_wall_requires_explicit_local_link_and_stays_on_same_input():
    from tools.dfm.ontology.targets import resolve_catalog_targets

    manifest, snapshot = discovered("screw_boss", "wall")
    boss = manifest.features[0]
    wall = replace(
        boss, feature_id="main.1", kind="main_wall", region_refs=["main.wall"]
    )
    local = replace(
        manifest.regions[0], region_id="main.wall", feature_refs=[wall.feature_id]
    )
    manifest.features.append(wall)
    manifest.regions.append(local)
    snapshot.feature_refs.append(wall.feature_id)
    snapshot.region_refs.append(local.region_id)
    spec = {
        "check_id": "C_BOSS",
        "alias": "reference",
        "metric_id": METRIC,
        "feature_kinds": ["screw_boss"],
        "operand_text": "相邻主体壁厚度",
    }
    with pytest.raises(DFMError):
        resolve_catalog_targets([spec], manifest, snapshot)
    boss.relationships.append({
        "relation": "attached_to",
        "target_ref": wall.feature_id,
        "region_refs": [local.region_id],
    })
    targets = resolve_catalog_targets(
        [spec, dict(spec, check_id="C_OTHER")], manifest, snapshot
    )
    assert len(targets) == 1
    assert len(targets[0]["operand_anchors"]) == 2
    manifest.regions[1] = replace(local, input_sha256="c" * 64)
    with pytest.raises(DFMError):
        resolve_catalog_targets([spec], manifest, snapshot)
