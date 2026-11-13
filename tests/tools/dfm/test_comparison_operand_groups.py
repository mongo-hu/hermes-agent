"""Explicit comparison operands partition selection, never infer formula intent."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.tools.dfm.test_catalog_alignment import (
    appearance_payload, gated_payload, measurement, operations, plan, rehash,
    wall_geometric_capability,
)
from tools.dfm.contracts import ArtifactRecord, PlanRecord
from tools.dfm.errors import DFMError
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.findings import materialize_evaluated_findings
from tools.dfm.ontology import LocalOntologyStore
from tools.dfm.reporting.result_assembler import materialize_result_reports


GIDS = {"outer": "G_WALL_THK_MIN", "inner": "G_WALL_THK_MAX", "reference": "G_WALL_FILLET_REF_T"}


def publication(*, deferred=False):
    value = gated_payload()
    check = value["rules"][0]["check_id"]
    relation = next(row for row in value["relations"] if row["predicate"] == "USES_OPERAND")
    geometric = next(row for row in value["concepts"] if row["concept_id"] == relation["object_id"])
    relation["qualifiers_json"].update(alias="outer", aggregation="identity")
    for alias in ("inner", "reference"):
        concept = deepcopy(geometric)
        concept["concept_id"] = GIDS[alias]
        value["concepts"].append(concept)
        extra = deepcopy(relation)
        extra.update(relation_id="rel." + alias, object_id=GIDS[alias])
        extra["qualifiers_json"]["alias"] = alias
        value["relations"].append(extra)
    original = value["rules"][0]
    value["rules"] = []
    for alias, threshold, priority in [("outer", .5, 100), ("inner", .25, 10)]:
        rule = deepcopy(original)
        rule.update(rule_id="R_" + alias, rule_version_id="RV_" + alias,
                    evaluation_gate_json=None, comparison_operand_alias=alias, priority=priority)
        rule["acceptance_criteria_json"] = [[{
            "criterion_id": alias, "expression": {"op": "divide", "args": [{"operand": alias}, {"operand": "reference"}]},
            "comparator": "GTE", "threshold": threshold, "result_unit": "ratio",
        }]]
        value["rules"].append(rule)
    if deferred:
        source = appearance_payload()
        value["concepts"].append(deepcopy(next(row for row in source["concepts"] if row["concept_id"] == "F_APPR_REGION_REQ")))
        req = deepcopy(next(row for row in source["relations"] if row["object_id"] == "F_APPR_REGION_REQ"))
        req["subject_id"] = check
        value["relations"].append(req)
        for concept in value["concepts"]:
            if concept["concept_id"] == check:
                concept["concept_id"] = "C_WALL_DRAFT"
        for row in value["relations"]:
            for key in ("subject_id", "object_id"):
                if row[key] == check:
                    row[key] = "C_WALL_DRAFT"
        for rule in value["rules"]:
            rule["check_id"] = "C_WALL_DRAFT"
            rule["conditions_json"].append({"factor_id": "F_APPR_REGION_REQ", "operator": "EQ", "value": "非外观"})
    return rehash(value)


def capabilities():
    capability = wall_geometric_capability()
    base = capability[GIDS["outer"]]
    for gid in GIDS.values():
        capability[gid] = deepcopy(base)
    for alias in ("outer", "inner"):
        capability[GIDS[alias]]["requires_annotations"] = ["appearance"]
    return capability


def compile_publication(value, capability=None):
    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings(capabilities() if capability is None else capability)
    return store, store.compile("injection", {"material": "ABS"}, operations())


def measured(outer=2, inner=1, reference=2, *, classified=False):
    return [replace(measurement(value), result_id=alias, geometric_id=GIDS[alias],
                    quality={"measurement_instance_id": "position", **({"appearance_class": "non_appearance"} if classified else {})})
            for alias, value in (("outer", outer), ("inner", inner), ("reference", reference)) if value is not None]


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("outer,inner,expected", [(2, .1, {"R_outer": "pass", "R_inner": "fail"}),
                                                   (.1, 1, {"R_outer": "fail", "R_inner": "pass"}),
                                                   (2, None, {"R_outer": "pass", "R_inner": "indeterminate"})])
def test_same_check_different_comparisons_keep_independent_results(deferred, outer, inner, expected):
    store, compiled = compile_publication(publication(deferred=deferred))
    assert {binding.comparison_operand_alias for binding in compiled.rule_bindings} == {"outer", "inner"}
    pinned = PlanRecord.from_dict(plan(compiled).to_dict())
    evaluated, _ = EvaluationEngine().evaluate(measured(outer, inner, classified=deferred), pinned)
    assert {row.rule_id: row.outcome for row in evaluated} == expected
    assert len({row.evaluation_id for row in evaluated}) == 2
    assert all(row.rule_id.endswith(row.comparison_operand_alias) for row in pinned.rule_bindings)


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("unsupported", ["outer", "inner"])
def test_one_unsupported_comparison_does_not_drop_other_group_or_factor_questions(deferred, unsupported):
    capability = capabilities()
    capability.pop(GIDS[unsupported])
    store, compiled = compile_publication(publication(deferred=deferred), capability)
    remaining = "R_inner" if unsupported == "outer" else "R_outer"
    assert [row.rule_id for row in compiled.rule_bindings] == [remaining]
    assert set(compiled.rules) == {remaining}
    assert compiled.skipped_checks[0].details["comparison_operand_alias"] == unsupported
    assert "material" in {row["name"] for row in store.fact_requirements("injection")}


def test_later_unsupported_variant_rolls_back_only_its_comparison_group():
    value = publication(deferred=True)
    value["rules"][0]["priority"] = 0
    relation = deepcopy(next(row for row in value["relations"] if row["predicate"] == "USES_OPERAND"))
    concept = deepcopy(next(row for row in value["concepts"] if row["concept_id"] == relation["object_id"]))
    concept["concept_id"] = "G_WALL_TRANS_T1"
    value["concepts"].append(concept)
    relation.update(relation_id="rel.extra", object_id=concept["concept_id"])
    relation["qualifiers_json"]["alias"] = "extra"
    value["relations"].append(relation)
    variant = deepcopy(value["rules"][1])
    variant.update(rule_id="R_inner_extra", rule_version_id="RV_inner_extra", priority=20)
    variant["acceptance_criteria_json"][0][0]["expression"]["args"][0] = {
        "op": "add", "args": [{"operand": "inner"}, {"operand": "extra"}]}
    value["rules"].append(variant)
    _, compiled = compile_publication(value)
    assert [row.rule_id for row in compiled.rule_bindings] == ["R_outer"]
    assert set(compiled.rules) == {"R_outer"}
    assert set(compiled.binding_selectors) == {compiled.rule_bindings[0].binding_id}


@pytest.mark.parametrize("deferred", [False, True])
def test_same_comparison_variants_keep_priority_selection_and_conflict_detection(deferred):
    value = publication(deferred=deferred)
    variant = deepcopy(value["rules"][1])
    variant.update(rule_id="R_inner_specific", rule_version_id="RV_inner_specific", priority=20)
    variant["acceptance_criteria_json"][0][0]["threshold"] = .4
    value["rules"].append(variant)
    store, compiled = compile_publication(value)
    evaluations, _ = EvaluationEngine().evaluate(measured(classified=deferred), plan(compiled))
    assert {row.rule_id for row in evaluations} == {"R_outer", "R_inner_specific"}
    value["rules"][1]["priority"] = 20
    with pytest.raises(DFMError) as failure:
        _, compiled = compile_publication(value)
        EvaluationEngine().evaluate(measured(classified=deferred), plan(compiled))
    assert failure.value.code == "ontology_rule_conflict"


def test_same_comparison_factor_variants_are_not_independent_objects():
    value = publication(deferred=True)
    variant = deepcopy(value["rules"][1])
    variant.update(rule_id="R_inner_appearance", rule_version_id="RV_inner_appearance", priority=1000)
    variant["conditions_json"][-1]["value"] = "外观"
    value["rules"].append(variant)
    _, compiled = compile_publication(value)
    evaluations, _ = EvaluationEngine().evaluate(measured(classified=True), plan(compiled))
    assert {row.rule_id for row in evaluations} == {"R_outer", "R_inner"}


@pytest.mark.parametrize("kind", ["missing_group", "unknown_alias", "gate_only_alias", "empty", "non_string", "schema4"])
def test_invalid_comparison_publication_fails_closed(kind):
    value = publication()
    rule = value["rules"][0]
    if kind == "missing_group":
        rule.pop("comparison_operand_alias")
    elif kind == "unknown_alias":
        rule["comparison_operand_alias"] = "unknown"
    elif kind == "gate_only_alias":
        rule["comparison_operand_alias"] = "inner"
        rule["evaluation_gate_json"] = {"expression": {"operand": "inner"}, "comparator": "GT", "threshold": 0, "result_unit": "mm"}
    elif kind == "empty":
        rule["comparison_operand_alias"] = ""
    elif kind == "non_string":
        rule["comparison_operand_alias"] = []
    else:
        value["schema_version"] = 4
        for row in value["rules"]:
            row.pop("evaluation_gate_json")
    with pytest.raises(DFMError) as failure:
        LocalOntologyStore.from_package(rehash(value))
    assert failure.value.code == "ontology_snapshot_invalid"


@pytest.mark.parametrize("schema", [4, 5])
def test_null_or_absent_comparison_preserves_rule_and_binding_hashes(schema):
    value = publication()
    value["schema_version"] = schema
    for row in value["rules"]:
        row.pop("comparison_operand_alias")
        if schema == 4:
            row.pop("evaluation_gate_json")
    store, first = compile_publication(value)
    before = store.check_context(value["rules"][0]["check_id"])["rules"]
    for row in value["rules"]:
        row["comparison_operand_alias"] = None
    store, second = compile_publication(value)
    after = store.check_context(value["rules"][0]["check_id"])["rules"]
    assert before == after
    assert [row.to_dict() for row in first.rule_bindings] == [row.to_dict() for row in second.rule_bindings]
    assert len(first.rule_bindings) == 1
    for compiled in (first, second):
        assert all("comparison_operand_alias" not in row.to_dict() for row in compiled.rule_bindings)


def test_existing_schema5_sqlite_without_comparison_column_still_compiles(tmp_path):
    value = gated_payload()
    path = tmp_path / "old-schema5.sqlite3"
    store = LocalOntologyStore.from_package(value, database_path=path)
    before = store.compile("injection", {"material": "ABS"}, operations())
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE rule_version DROP COLUMN comparison_operand_alias")
        connection.execute("UPDATE snapshot_metadata SET database_schema_version=4")
    after = store.compile("injection", {"material": "ABS"}, operations())
    assert [row.to_dict() for row in before.rule_bindings] == [row.to_dict() for row in after.rule_bindings]


def test_explicit_comparison_changes_content_and_binding_semantic_hash():
    value = publication()
    store, compiled = compile_publication(value)
    rule_hashes = {row["rule_id"]: row["content_sha256"] for row in store.check_context(value["rules"][0]["check_id"])["rules"]}
    binding = next(row for row in compiled.rule_bindings if row.rule_id == "R_outer")
    kwargs = {"rule_version": "test", "expected": .5, "expected_unit": "ratio"}
    assert EvaluationEngine._rule_hash(binding=binding, **kwargs) != EvaluationEngine._rule_hash(
        binding=replace(binding, comparison_operand_alias=None), **kwargs)
    value["rules"][0]["comparison_operand_alias"] = "reference"
    changed, _ = compile_publication(value)
    after = {row["rule_id"]: row["content_sha256"] for row in changed.check_context(value["rules"][0]["check_id"])["rules"]}
    assert rule_hashes["R_outer"] != after["R_outer"]
    assert rule_hashes["R_inner"] == after["R_inner"]


@pytest.mark.parametrize("alias", ["", "missing"])
def test_plan_binding_rejects_unbound_or_gate_only_comparison(alias):
    _, compiled = compile_publication(publication())
    binding = compiled.rule_bindings[0]
    with pytest.raises(DFMError):
        replace(binding, comparison_operand_alias=alias).validate()


def test_two_failed_comparisons_keep_separate_findings_reports_and_evidence(tmp_path):
    _, compiled = compile_publication(publication())
    pinned = plan(compiled)
    measurements = measured(.1, .1)
    evaluated, provenance = EvaluationEngine().evaluate(measurements, pinned)
    payloads = {"geometric_results": {"input_sha256": "a" * 64, "results": [row.to_dict() for row in measurements]},
                "evaluations": {"evaluations": [row.to_dict() for row in evaluated], "provenance": provenance},
                "evidence_records": {"records": [{"evidence_id": "evidence-" + row.rule_id, "evaluation_ids": [row.evaluation_id]} for row in evaluated]}}
    artifacts = []
    for kind, payload in payloads.items():
        path = kind + ".json"
        (tmp_path / path).write_text(json.dumps(payload), encoding="utf-8")
        artifacts.append(ArtifactRecord(kind, kind, path, "application/json", "now"))
    findings = materialize_evaluated_findings(tmp_path, artifacts)
    assert len(findings) == 2 and len({row.finding_id for row in findings}) == 2
    assert {tuple(row.evidence_refs) for row in findings} == {("evidence-R_outer",), ("evidence-R_inner",)}
    assert {tuple(row.measurement_ids) for row in findings} == {("outer", "reference"), ("inner", "reference")}
    (tmp_path / "runs/run/artifacts").mkdir(parents=True)
    outputs = materialize_result_reports(tmp_path, "run", pinned, artifacts)
    report = json.loads((tmp_path / next(row.relative_path for row in outputs if row.kind == "report_json")).read_text(encoding="utf-8"))
    assert {issue["metric"]["rule_id"] for issue in report["issues"]} == {"R_outer", "R_inner"}
