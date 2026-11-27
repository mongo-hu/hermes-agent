"""Reviewed Excel meanings must survive Worker binding and instance evaluation."""

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tests.tools.dfm.test_catalog_alignment import (
    measurement, operations, plan, rehash, wall_geometric_capability,
)
from tests.tools.dfm.test_comparison_operand_groups import publication
from tests.tools.dfm.test_geometric_instances import gated_model
from tools.dfm.contracts import (
    EffectiveRule, FeatureRecord, GeometricRequest, GeometricRuleOperand, PlanRecord,
)
from tools.dfm.errors import DFMError
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.ontology import LocalOntologyStore
from tools.dfm.processes.base import ProcessPlan
from tools.dfm.processes.occt_injection import geometry_binding_index
from tools.dfm.runtime.process import GeometrySession
from tools.dfm.service import DFMService


RADIUS = "G_HOLLOW_BOSS_FILLET_BOTTOM_R"
REFERENCE = "G_HOLLOW_BOSS_FILLET_REF_THK"
HOLE_CONTEXT = {"fillet_location": "hole_bottom"}


def boss_publication():
    value = publication()
    replacements = {"G_WALL_THK_MIN": RADIUS, "G_WALL_THK_MAX": RADIUS,
                    "G_WALL_FILLET_REF_T": REFERENCE}
    concepts = []
    for concept in value["concepts"]:
        if concept["concept_id"] == "G_WALL_THK_MAX":
            continue
        concept["concept_id"] = replacements.get(concept["concept_id"], concept["concept_id"])
        if concept["concept_type"] == "feature_type":
            concept["properties_json"]["feature_kind"] = "hollow_boss"
        concepts.append(concept)
    value["concepts"] = concepts
    for relation in value["relations"]:
        relation["object_id"] = replacements.get(relation["object_id"], relation["object_id"])
        if relation["predicate"] == "USES_OPERAND" and relation["qualifiers_json"]["alias"] == "inner":
            relation["qualifiers_json"]["measurement_context"] = dict(HOLE_CONTEXT)
    return rehash(value)


def capabilities(*, location=True, version="2.13.0"):
    base = wall_geometric_capability("hollow_boss")["G_WALL_THK_MIN"]
    result = {key: deepcopy(base) for key in (RADIUS, REFERENCE)}
    for declaration in result.values():
        declaration["schema_bundle_version"] = version
    if location:
        result[RADIUS]["context_schema"]["properties"] = {
            "fillet_location": {"type": "string", "enum": ["outer_root", "hole_bottom"]},
        }
    return result


def compile_boss(value=None, capability=None):
    store = LocalOntologyStore.from_package(rehash(value or boss_publication()))
    store.configure_geometric_bindings(capability or capabilities())
    return store, store.compile("injection", {"material": "ABS"}, operations())


@pytest.mark.parametrize("root,hole,expected", [
    (2, .1, {"R_outer": "pass", "R_inner": "fail"}),
    (.1, 1, {"R_outer": "fail", "R_inner": "pass"}),
    (2, None, {"R_outer": "pass", "R_inner": "indeterminate"}),
])
def test_boss_locations_keep_independent_results_under_one_public_g(root, hole, expected):
    _, compiled = compile_boss()
    pinned = plan(compiled)
    fragment = pinned.plan_fragments[0]
    pinned = replace(pinned, plan_fragments=[replace(fragment, items=[
        *fragment.items, GeometricRequest("feature-main-wall-1", [RADIUS], HOLE_CONTEXT),
    ])])
    pinned = PlanRecord.from_dict(pinned.to_dict())
    assert {row.comparison_operand_alias: row.context for row in pinned.rule_bindings} == {
        "outer": {}, "inner": HOLE_CONTEXT,
    }
    rows = [replace(measurement(root), geometric_id=RADIUS, result_id="root"),
            replace(measurement(2), geometric_id=REFERENCE, result_id="reference")]
    if hole is not None:
        rows.append(replace(measurement(hole), geometric_id=RADIUS,
                            result_id="hole", context=HOLE_CONTEXT))
    evaluated, _ = EvaluationEngine().evaluate(rows, pinned)
    assert {row.rule_id: row.outcome for row in evaluated} == expected
    for row in evaluated:
        assert ("hole" not in row.measurement_ids if row.rule_id == "R_outer"
                else "root" not in row.measurement_ids)


def test_worker_without_location_capability_keeps_root_but_exposes_hole_gap():
    _, compiled = compile_boss(capability=capabilities(location=False))
    assert [row.rule_id for row in compiled.rule_bindings] == ["R_outer"]
    assert compiled.skipped_checks[0].reason == "geometric_context_unsupported"
    assert compiled.skipped_checks[0].details["comparison_operand_alias"] == "inner"


@pytest.mark.parametrize("context", [
    {"fillet_location": "unknown"}, {"fillet_location": 0}, {"undeclared": "hole_bottom"},
    {"draw_direction": [0, 0, 1]},
])
def test_unreviewed_or_unsupported_context_never_falls_back_to_root(context):
    value = boss_publication()
    relation = next(row for row in value["relations"] if row["relation_id"] == "rel.inner")
    relation["qualifiers_json"]["measurement_context"] = context
    _, compiled = compile_boss(value)
    assert [row.rule_id for row in compiled.rule_bindings] == ["R_outer"]
    assert compiled.skipped_checks[0].reason == "geometric_context_unsupported"


@pytest.mark.parametrize("invalid", [None, False, [], "hole_bottom"])
def test_publication_rejects_non_object_context(invalid):
    value = boss_publication()
    relation = next(row for row in value["relations"] if row["relation_id"] == "rel.inner")
    relation["qualifiers_json"]["measurement_context"] = invalid
    with pytest.raises(DFMError) as failure:
        LocalOntologyStore.from_package(rehash(value))
    assert failure.value.code == "ontology_snapshot_invalid"


@pytest.mark.parametrize("version,supported", [("2.11.0", False), ("", False),
                                                ("2.13.0", True), ("2.14.0", True)])
def test_reviewed_semantic_requirement_rejects_old_or_unidentified_worker(version, supported):
    value = boss_publication()
    relation = next(row for row in value["relations"] if row["relation_id"] == "rel.inner")
    relation["qualifiers_json"]["minimum_schema_bundle_version"] = "2.13.0"
    _, compiled = compile_boss(value, capabilities(version=version))
    assert {row.rule_id for row in compiled.rule_bindings} == (
        {"R_outer", "R_inner"} if supported else {"R_outer"})
    if not supported:
        assert compiled.skipped_checks[0].reason == "geometric_semantics_unsupported"


def test_context_survives_discovery_binding_and_worker_request_partition():
    store, compiled = compile_boss()
    scope = {"contract_version": "dfm.geometry/v2", "schema_bundle_version": "2.13.0",
             "geometrics": [{"geometric_id": gid, "support_status": "supported", **row}
                            for gid, row in capabilities().items()]}
    assert geometry_binding_index(scope)[RADIUS]["schema_bundle_version"] == "2.13.0"
    feature = FeatureRecord("boss-1", "hollow_boss", [], 1.0, input_sha256="a" * 64, status="confirmed")
    runtime = SimpleNamespace(occt_geometry_capability=scope, ontology_store=store)
    process = ProcessPlan("injection", "test", "test", "test", compiled.rules, [], {"step"},
                          rule_bindings=compiled.rule_bindings, binding_selectors=compiled.binding_selectors)
    bound = DFMService._bind_geometric_scope(runtime, process,
        SimpleNamespace(features=[feature], capabilities={}),
        SimpleNamespace(feature_refs=["boss-1"], worker_snapshot_id="snapshot", forming_scopes=[]))
    assert len(bound.rule_bindings) == 2
    assert {row.comparison_operand_alias: row.context for row in bound.rule_bindings} == {
        "outer": {}, "inner": HOLE_CONTEXT,
    }
    assert all(row.additional_operands[0].context == {} for row in bound.rule_bindings)
    requests = {(operand.geometric_id, tuple(operand.context.items()))
                for row in bound.rule_bindings for operand in row.measurement_operands()}
    assert requests == {(RADIUS, ()), (RADIUS, tuple(HOLE_CONTEXT.items())), (REFERENCE, ())}


@pytest.mark.parametrize("version", ["2.11.0", "2.13.0"])
def test_stdio_handshake_accepts_legacy_and_candidate_bundles(tmp_path, version):
    # Exercise the real process/JSON-RPC boundary with the hermetic Worker fixture.
    source = Path(__file__).parent / "fixtures" / "worker_fixture.py"
    fixture = tmp_path / "worker.py"
    fixture.write_text(source.read_text(encoding="utf-8").replace(
        '"schema_bundle_version": "2.11.0"', f'"schema_bundle_version": "{version}"'), encoding="utf-8")
    wrapper = tmp_path / ("worker.cmd" if os.name == "nt" else "worker")
    wrapper.write_text(
        f'@echo off\n"{sys.executable}" "{fixture}" session_v2 %*\n' if os.name == "nt"
        else f'#!/bin/sh\nexec "{sys.executable}" "{fixture}" session_v2 "$@"\n', encoding="utf-8")
    if os.name != "nt":
        wrapper.chmod(0o755)
    session = GeometrySession(str(wrapper), request_timeout_seconds=10)
    try:
        assert session.capability["schema_bundle_version"] == version
        assert session.call("capabilities.get", {})["contract_version"] == "dfm.geometry/v2"
    finally:
        session.close()


def test_local_transition_step_failure_does_not_contaminate_other_locations():
    pinned, rows = gated_model()
    binding = pinned.rule_bindings[0]
    step = GeometricRuleOperand("steps", "G_WALL_STEP_COUNT", feature_refs=["wall"])
    no_step = {"criterion_id": "no_step", "expression": {"operand": "steps"},
               "comparator": "EQ", "threshold": 0, "result_unit": "count"}
    binding = replace(binding, additional_operands=[*binding.additional_operands, step],
                      acceptance_criteria_json=[[no_step, binding.acceptance_criteria_json[0][0]]],
                      geometric_id="G_WALL_STEP_COUNT", operand_alias="steps", operator="==",
                      expression={"operand": "steps"})
    # Keep one declaration of each operand after making local step the primary.
    binding = replace(binding, additional_operands=[
        GeometricRuleOperand("delta", "G_WALL_TRANS_DT", feature_refs=["wall"]),
        *[item for item in binding.additional_operands if item.alias != "steps"],
    ])
    pinned = replace(pinned, rule_bindings=[binding], plan_fragments=[],
                     rules={"rule": EffectiveRule(0, "count", "fixture")})
    rows = [replace(row, value=4 if row.geometric_id == "G_WALL_TRANS_LEN" else 1) for row in rows]
    for instance, count in [("first", 1), ("second", 0)]:
        rows.append(replace(rows[0], result_id="steps-" + instance, geometric_id="G_WALL_STEP_COUNT",
                            value=count, unit="count", quality={"measurement_instance_id": instance}))
    evaluated, provenance = EvaluationEngine().evaluate(rows, pinned)
    assert {provenance[row.evaluation_id]["measurement_instance_id"]: row.outcome for row in evaluated} == {
        "first": "fail", "second": "pass",
    }
