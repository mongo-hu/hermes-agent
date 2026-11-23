"""V1.3.0 consumption keeps missing input, coverage and independent results distinct."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.tools.dfm.test_catalog_alignment import (
    appearance_payload, gated_payload, measurement, operations, plan, rehash,
    wall_geometric_capability,
)
from tests.tools.dfm.test_comparison_operand_groups import (
    capabilities, compile_publication, measured, publication,
)
from tests.tools.dfm.test_service import STEP_PAYLOAD, confirm_step_facts, service
from tools.dfm.contracts import Capability, CapabilityStatus, FactRecord, FeatureRecord
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.ontology import LocalOntologyStore
from tools.dfm.processes.occt_injection import geometry_binding_index


@pytest.mark.parametrize("facts,reason", [
    ({}, "required_factor_missing"),
    ({"material": None}, "required_factor_missing"),
    ({"material": "UNLISTED"}, "factor_combination_uncovered"),
])
def test_unmatched_factors_are_explicit_and_not_a_pass(facts, reason):
    value = gated_payload()
    next(item for item in value["concepts"] if item["concept_id"] == "factor.material")["properties_json"]["default_value"] = None
    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings(wall_geometric_capability())
    compiled = store.compile("injection", facts, operations())
    assert not compiled.rule_bindings
    assert compiled.skipped_checks[0].reason == reason
    if reason == "required_factor_missing":
        assert compiled.skipped_checks[0].details["missing_factor_ids"] == ["factor.material"]
    else:
        assert "missing_factor_ids" not in compiled.skipped_checks[0].details


@pytest.mark.parametrize("feature_kind,excluded,expected", [
    ("main_wall", False, True), ("hollow_boss", False, False),
    ("main_wall", True, False),
])
def test_only_discovered_applicable_features_get_factor_coverage_gaps(service, feature_kind, excluded, expected):
    dfm, _ = service
    store = LocalOntologyStore.from_package(gated_payload())
    store.configure_geometric_bindings(wall_geometric_capability())
    dfm.ontology_store = store
    compiled = store.compile("injection", {"material": "UNLISTED"}, operations())
    feature = FeatureRecord("feature.current", feature_kind, [], 1.0,
                            input_sha256="a" * 64, status="rejected" if excluded else "confirmed")
    manifest = SimpleNamespace(features=[feature], capabilities={})
    snapshot = SimpleNamespace(feature_refs=[feature.feature_id])
    scoped = dfm._bind_geometric_scope(compiled, manifest, snapshot)
    assert bool(scoped.skipped_checks) is expected
    if expected:
        assert scoped.skipped_checks[0].details["feature_refs"] == [feature.feature_id]


@pytest.mark.parametrize("check_id", ["C_PAD_ROOT_FILLET", "C_EXTRA_ADD_ROOT_THK"])
def test_published_instance_factor_selects_scoped_answers_without_claiming_worker_annotation_support(check_id):
    value = appearance_payload(check_id=check_id)
    factor = next(item for item in value["concepts"] if item["concept_id"] == "F_APPR_REGION_REQ")
    factor["properties_json"]["impact_scope"] = "特征实例级"
    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings(wall_geometric_capability())
    requirement = next(item for item in store.fact_requirements("injection") if item["name"] == "appr_region_req")
    assert requirement["impact_scope"] == "特征实例级"
    for appearance, expected in [("外观", "fail"), ("非外观", "pass")]:
        compiled = store.compile("injection", {"material": "ABS", "appr_region_req": "非外观"},
                                 operations(), scoped_facts={check_id: {"appr_region_req": appearance}})
        assert len(compiled.rule_bindings) == 1
        assert compiled.rule_bindings[0].rule_selection is None
        evaluated, _ = EvaluationEngine().evaluate([measurement(1.5)], plan(compiled))
        assert evaluated[0].rule_id == "R_" + appearance
        assert evaluated[0].outcome == expected


def test_measured_appearance_without_published_branch_is_indeterminate():
    value = appearance_payload()
    value["rules"] = [value["rules"][1]]  # Published non-appearance branch only.
    store = LocalOntologyStore.from_package(rehash(value))
    capability = wall_geometric_capability()
    capability["G_WALL_THK_MIN"]["requires_annotations"] = ["appearance"]
    store.configure_geometric_bindings(capability)
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    row = replace(measurement(1.5), quality={
        "measurement_instance_id": "position", "appearance_class": "appearance",
    })
    results, provenance = EvaluationEngine().evaluate([row], plan(compiled))
    assert len(results) == 1
    assert results[0].outcome == "indeterminate"
    assert results[0].rule_selection_result["reason_codes"] == ["factor_combination_uncovered"]
    assert provenance[results[0].evaluation_id]["acceptance_skipped"]


@pytest.mark.parametrize("mixed_features", [False, True])
def test_service_exposes_uncovered_feature_without_hiding_valid_instance(service, monkeypatch, mixed_features):
    dfm, temp = service
    value = appearance_payload(check_id="C_PAD_ROOT_FILLET")
    value["rules"] = [value["rules"][1]]
    factor = next(item for item in value["concepts"] if item["concept_id"] == "F_APPR_REGION_REQ")
    factor["properties_json"].update(impact_scope="特征实例级", default_value=None)
    dfm.ontology_store.install_package(rehash(value))
    capability = {"contract_version": "dfm.geometry/v2", "engine_version": "fixture",
                  "schema_bundle_version": "fixture", "dependency_fingerprint": "f" * 64, "geometrics": [{
        "geometric_id": "G_WALL_THK_MIN", "support_status": "supported",
        **wall_geometric_capability()["G_WALL_THK_MIN"],
    }]}

    class AvailableOcctAnalyzer:
        key, version, supported_inputs = "occt_cpp", "test", ("step",)

        @staticmethod
        def capability(_context):
            return Capability("occt_cpp", CapabilityStatus.AVAILABLE, "available for test")

    dfm.registry.register(AvailableOcctAnalyzer())
    dfm.occt_geometry_capability = capability
    dfm.ontology_store.configure_geometric_bindings(geometry_binding_index(capability))
    monkeypatch.setattr(dfm, "_ensure_occt_geometry_capability", lambda _project_id: capability)
    project_id = dfm.project("create", name="Factor coverage")["project_id"]
    source = temp / "factor-coverage.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    confirm_step_facts(dfm, project_id)
    dfm.analysis("discover", project_id=project_id)
    store = dfm._store(project_id)
    manifest = store.load()
    snapshot = manifest.discovery_snapshots[-1]
    feature_ids = ["feature.uncovered"] + (["feature.covered"] if mixed_features else [])
    features = [FeatureRecord(identity, "main_wall", [], 1.0, status="confirmed") for identity in feature_ids]
    facts = [FactRecord("fact." + identity, "appr_region_req",
                        "外观" if identity == "feature.uncovered" else "非外观",
                        "test_user", "confirmed", feature_id=identity)
             for identity in feature_ids]
    store.update(lambda current: replace(current, features=features, facts=[*current.facts, *facts],
                 discovery_snapshots=[replace(snapshot, feature_refs=feature_ids,
                                              worker_snapshot_id="snapshot.catalog")]),
                 expected_revision=manifest.revision)
    result = dfm.analysis("plan", project_id=project_id, analyzer_key="occt_cpp")
    gaps = result["plan"]["coverage_gaps"] if mixed_features else result["coverage_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["reason"] == "factor_combination_uncovered"
    assert gaps[0]["status"] == "unsupported"
    assert gaps[0]["feature_refs"] == ["feature.uncovered"]
    if mixed_features:
        assert {tuple(row["feature_refs"]) for row in result["plan"]["rule_bindings"]} == {("feature.covered",)}
        assert dfm._store(project_id).load().plans[-1].coverage_gaps == gaps
    else:
        assert result["status"] == "unsupported_checks"
        assert result["reason_code"] == "applicable_checks_skipped"
        from tools import dfm_tool

        monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: dfm)
        delivered = json.loads(dfm_tool._call("analysis", {
            "action": "plan", "project_id": project_id, "analyzer_key": "occt_cpp",
        }))
        assert delivered["status"] == "unsupported_checks"
        assert delivered["reason_code"] == "applicable_checks_skipped"
        assert delivered["coverage_gaps"] == gaps


@pytest.mark.parametrize("outer,inner,expected", [
    (2, .1, {"R_outer": "pass", "R_inner": "fail"}),
    (.1, 1, {"R_outer": "fail", "R_inner": "pass"}),
    (2, None, {"R_outer": "pass", "R_inner": "indeterminate"}),
    (2, 0, {"R_outer": "pass", "R_inner": "fail"}),
])
def test_external_root_and_hole_bottom_fillet_remain_independent(outer, inner, expected):
    value = publication()
    replacements = {
        "G_WALL_THK_MIN": "G_HOLLOW_BOSS_FILLET_BOTTOM_R",
        "G_WALL_THK_MAX": "G_HOLLOW_BOSS_HOLE_BOTTOM_FILLET_R",
        "G_WALL_FILLET_REF_T": "G_HOLLOW_BOSS_WALL_REF_THK",
    }
    for concept in value["concepts"]:
        concept["concept_id"] = replacements.get(concept["concept_id"], concept["concept_id"])
    for relation in value["relations"]:
        relation["object_id"] = replacements.get(relation["object_id"], relation["object_id"])
    caps = {replacements.get(key, key): declaration for key, declaration in capabilities().items()}
    _, compiled = compile_publication(value, caps)
    rows = [replace(row, geometric_id=replacements[row.geometric_id]) for row in measured(outer, inner)]
    evaluated, _ = EvaluationEngine().evaluate(rows, plan(compiled))
    assert {row.rule_id: row.outcome for row in evaluated} == expected


@pytest.mark.parametrize("gid", ["G_HOLLOW_BOSS_BOTTOM_THK", "G_HOLLOW_BOSS_HOLE_BOTTOM_FILLET_R"])
def test_through_hole_bottom_not_applicable_is_not_a_hard_failure(gid):
    value = gated_payload()
    for concept in value["concepts"]:
        if concept["concept_id"] == "G_WALL_THK_MIN":
            concept["concept_id"] = gid
    for relation in value["relations"]:
        if relation["object_id"] == "G_WALL_THK_MIN":
            relation["object_id"] = gid
    value["rules"][0]["evaluation_gate_json"] = None
    caps = {gid: wall_geometric_capability()["G_WALL_THK_MIN"]}
    _, compiled = compile_publication(value, caps)
    row = replace(measurement(None), geometric_id=gid, status="not_applicable",
                  reason="through_hole_has_no_bottom")
    evaluated, _ = EvaluationEngine().evaluate([row], plan(compiled))
    assert evaluated[0].outcome == "indeterminate"
    assert evaluated[0].actual is None
    unavailable = evaluated[0].operand_values["actual"]
    assert unavailable["reason"]["details"]["results"][0]["status"] == "not_applicable"


@pytest.mark.parametrize("steps,chord,outcome", [(1, None, "fail"), (0, None, "indeterminate"), (0, 4, "pass")])
def test_transition_failed_step_is_not_hidden_by_missing_chord(steps, chord, outcome):
    value = publication()
    value["rules"] = [value["rules"][0]]
    rule = value["rules"][0]
    rule["evaluation_gate_json"] = {
        "expression": {"operand": "reference"}, "comparator": "GT", "threshold": .1, "result_unit": "mm",
    }
    ratio = {"op": "divide", "args": [{"operand": "inner"}, {"operand": "reference"}]}
    rule["acceptance_criteria_json"] = [[
        {"criterion_id": "no_step", "expression": {"operand": "outer"}, "comparator": "EQ", "threshold": 0, "result_unit": "1"},
        {"criterion_id": "chord_ratio", "expression": {"op": "multiply", "args": [ratio, ratio]},
         "comparator": "GTE", "threshold": 10, "result_unit": "ratio"},
    ]]
    step = next(item for item in value["concepts"] if item["concept_id"] == "G_WALL_THK_MIN")
    step["properties_json"] = {"dimension": "dimensionless", "canonical_unit": "1"}
    _, compiled = compile_publication(value)
    rows = [replace(row, unit="1" if row.geometric_id == "G_WALL_THK_MIN" else "mm")
            for row in measured(steps, chord, 1)]
    evaluated, _ = EvaluationEngine().evaluate(rows, plan(compiled))
    assert evaluated[0].outcome == outcome
    caps = capabilities()
    caps.pop("G_WALL_THK_MAX")
    _, unavailable = compile_publication(value, caps)
    assert not unavailable.rule_bindings
    assert unavailable.skipped_checks[0].reason == "geometric_id_unsupported"


@pytest.mark.parametrize("threshold", [45, 120])
@pytest.mark.parametrize("offset,outcome", [(-.001, "fail"), (0, "fail"), (.001, "pass")])
def test_strict_angle_boundaries_use_published_gt(threshold, offset, outcome):
    value = gated_payload()
    value["rules"][0].update(evaluation_gate_json=None, conditions_json=[])
    value["rules"][0]["acceptance_criteria_json"] = [[{
        "criterion_id": "angle", "expression": {"operand": "actual"},
        "comparator": "GT", "threshold": threshold, "result_unit": "degree",
    }]]
    geometric = next(row for row in value["concepts"] if row["concept_id"] == "G_WALL_THK_MIN")
    geometric["properties_json"] = {"dimension": "angle", "canonical_unit": "degree"}
    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings(wall_geometric_capability())
    compiled = store.compile("injection", {}, operations())
    evaluated, _ = EvaluationEngine().evaluate([measurement(threshold + offset, "degree")], plan(compiled))
    assert evaluated[0].outcome == outcome


@pytest.mark.parametrize("reference", [1e-9, 1e-15])
def test_small_positive_denominator_is_not_treated_as_zero(reference):
    value = publication()
    value["rules"] = [value["rules"][0]]
    ratio = {"op": "divide", "args": [{"operand": "outer"}, {"operand": "reference"}]}
    value["rules"][0]["acceptance_criteria_json"] = [[{
        "criterion_id": "chord_ratio", "expression": {"op": "multiply", "args": [ratio, ratio]},
        "comparator": "GTE", "threshold": 10, "result_unit": "ratio",
    }]]
    _, compiled = compile_publication(value)
    evaluated, _ = EvaluationEngine().evaluate(measured(4 * reference, None, reference), plan(compiled))
    assert evaluated[0].outcome == "pass"


@pytest.mark.parametrize("target", [0, -0.0])
@pytest.mark.parametrize("actual", [0, -0.0, 1e-12, -1e-12, 1e-15, -1e-15, 5e-324, -5e-324])
def test_zero_equality_never_absorbs_nonzero_geometry(target, actual):
    assert EvaluationEngine._compare("==", actual, target, binding_id="zero-state") is (actual == 0)
    assert EvaluationEngine._compare("!=", actual, target, binding_id="zero-state") is (actual != 0)


@pytest.mark.parametrize("actual,target", [(1 + 1e-13, 1), (-1 - 1e-13, -1), (1e-15, 2e-15)])
def test_nonzero_equality_keeps_existing_numeric_tolerance(actual, target):
    assert EvaluationEngine._compare("==", actual, target, binding_id="numeric-threshold")
    assert not EvaluationEngine._compare("!=", actual, target, binding_id="numeric-threshold")
