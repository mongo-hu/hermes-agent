"""Published Mold catalog -> SQLite -> pinned Plan -> Measurement evaluation."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator

from tools.dfm.contracts import (
    GeometricRequest,
    GeometricResultRecord,
    PlanFragmentRecord,
    PlanOperation,
    PlanRecord,
)
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


def composite_payload():
    value = payload()
    value["schema_version"] = 3
    for concept in value["concepts"]:
        for field in ("aliases", "data_schema", "properties"):
            if field in concept:
                concept[f"{field}_json"] = concept.pop(field)
    for relation in value["relations"]:
        relation["qualifiers_json"] = relation.pop("qualifiers")
    for option in value["factor_options"]:
        option["value_json"] = option.pop("value")
    rule = value["rules"][0]
    rule["conditions_json"] = rule.pop("conditions")
    for legacy_field in ("expression", "comparator", "threshold", "result_unit"):
        rule.pop(legacy_field)
    rule["acceptance_criteria_json"] = [
        {
            "criterion_id": "wall_minimum",
            "expression": {"operand": "actual"},
            "comparator": "GTE",
            "threshold": 1.2,
            "result_unit": "mm",
        },
        {
            "criterion_id": "wall_maximum",
            "expression": {"operand": "actual"},
            "comparator": "LTE",
            "threshold": 2.0,
            "result_unit": "mm",
        },
    ]
    rule["severity"] = "medium"
    rule["severity_rationale"] = "Manufacturing stability requires both limits."
    return rehash(value)


def grouped_payload():
    value = composite_payload()
    value["schema_version"] = 4
    rule = value["rules"][0]
    bounded_group = deepcopy(rule["acceptance_criteria_json"])
    alternate = deepcopy(bounded_group[0])
    alternate.update(
        criterion_id="wall_alternate_minimum",
        threshold=3.0,
    )
    rule["acceptance_criteria_json"] = [bounded_group, [alternate]]
    rule["severity_rationale"] = "Either released engineering range is acceptable."
    return rehash(value)


def test_schema_4_factor_impact_scope_is_validated_and_projected(tmp_path):
    value = grouped_payload()
    factor = next(
        item for item in value["concepts"] if item["concept_type"] == "factor"
    )
    factor["properties_json"]["impact_scope"] = "特征实例级"
    value = rehash(value)

    store = LocalOntologyStore(tmp_path / "impact-scope.sqlite3")
    store.install_package(value)

    definition = next(
        item
        for item in store.factor_definitions("injection")
        if item["concept_id"] == factor["concept_id"]
    )
    requirement = next(
        item
        for item in store.fact_requirements("injection")
        if item["name"] == factor["properties_json"]["runtime_key"]
    )
    assert definition["impact_scope"] == "特征实例级"
    assert requirement["impact_scope"] == "特征实例级"


def test_schema_4_rejects_unknown_factor_impact_scope(tmp_path):
    value = grouped_payload()
    factor = next(
        item for item in value["concepts"] if item["concept_type"] == "factor"
    )
    factor["properties_json"]["impact_scope"] = "check_level"
    value = rehash(value)

    with pytest.raises(DFMError) as exc_info:
        LocalOntologyStore(tmp_path / "invalid-impact-scope.sqlite3").install_package(
            value
        )

    assert exc_info.value.code == "ontology_snapshot_invalid"
    assert exc_info.value.details["path"][-1] == "impact_scope"


def legacy_schema4_payload(kind="wall"):
    value = grouped_payload()
    for concept in value["concepts"]:
        if concept["concept_type"] == "feature_type":
            concept["properties_json"] = {"worker_kind": kind}
        elif concept["concept_type"] == "geometric":
            concept["properties_json"] = {
                "worker_geometric_id": "",
                "quantity_id": "",
                "dimension": "",
                "canonical_unit": "",
            }
    return rehash(value)


def wall_geometric_capability(kind="main_wall"):
    return {
        "G_WALL_THK_MIN": {
            "feature_kind": kind,
            "scope_modes": ["feature"],
            "direction_modes": ["none"],
            "requires_complete_inventory": [],
            "requires_relationships": [],
            "context_schema": {"type": "object", "additionalProperties": False},
        }
    }


@pytest.mark.parametrize("kind,worker_kind", [("wall", "main_wall"), ("hollow_boss", "hollow_boss")])
def test_legacy_schema4_preserves_publication_and_compiles_with_v2_capability(tmp_path, kind, worker_kind):
    value = legacy_schema4_payload(kind)
    original = deepcopy(value)
    store = LocalOntologyStore(tmp_path / "legacy.sqlite3")
    store.install_package(value)
    store.configure_geometric_bindings(wall_geometric_capability(worker_kind))

    compiled = store.compile("injection", {"material": "ABS"}, operations())
    assert compiled.rule_bindings
    assert not compiled.skipped_checks
    assert compiled.identity.content_sha256 == original["content_sha256"]
    assert store.geometric_scope_specs("injection")[0]["feature_kind"] == worker_kind
    context = store.check_context(value["rules"][0]["check_id"])
    for relation in context["relations"]:
        if relation["predicate"] in {"APPLIES_TO_FEATURE", "USES_OPERAND"}:
            concept = next(item for item in original["concepts"] if item["concept_id"] == relation["object"]["concept_id"])
            assert relation["object"]["properties_json"] == concept["properties_json"]
    assert value == original


@pytest.mark.parametrize("change", [
    "feature_extra", "feature_mixed", "geometric_extra", "geometric_missing",
    "new_feature_old_geometry", "old_feature_new_geometry", "non_string", "schema_3",
])
def test_legacy_schema4_rejects_partial_or_unknown_property_profiles(tmp_path, change):
    store = LocalOntologyStore(tmp_path / "strict.sqlite3")
    store.install_package(grouped_payload())
    original = store.identity()
    value = legacy_schema4_payload()
    feature = next(item for item in value["concepts"] if item["concept_type"] == "feature_type")
    geometric = next(item for item in value["concepts"] if item["concept_type"] == "geometric")
    if change == "feature_extra":
        feature["properties_json"]["unknown"] = "wall"
    elif change == "feature_mixed":
        feature["properties_json"]["feature_kind"] = "main_wall"
    elif change == "geometric_extra":
        geometric["properties_json"]["worker_role"] = "wall"
    elif change == "geometric_missing":
        geometric["properties_json"].pop("quantity_id")
    elif change == "new_feature_old_geometry":
        feature["properties_json"] = {"feature_kind": "main_wall"}
    elif change == "old_feature_new_geometry":
        geometric["properties_json"] = {"dimension": "length", "canonical_unit": "mm"}
    elif change == "non_string":
        geometric["properties_json"]["quantity_id"] = None
    else:
        value["schema_version"] = 3
        value["rules"][0]["acceptance_criteria_json"] = value["rules"][0]["acceptance_criteria_json"][0]
    with pytest.raises(DFMError) as exc_info:
        store.install_package(rehash(value))
    assert exc_info.value.code == "ontology_snapshot_invalid"
    assert store.identity() == original


def test_current_wall_kind_uses_runtime_inventory_without_rewriting_publication(tmp_path):
    value = grouped_payload()
    for concept in value["concepts"]:
        if concept["concept_type"] == "feature_type":
            concept["properties_json"] = {"feature_kind": "wall"}
    value = rehash(value)
    original = deepcopy(value)
    store = LocalOntologyStore(tmp_path / "current-wall.sqlite3")
    store.install_package(value)
    store.configure_geometric_bindings(wall_geometric_capability())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    assert compiled.rule_bindings
    assert not compiled.skipped_checks
    assert store.geometric_scope_specs("injection")[0]["feature_kind"] == "main_wall"
    assert compiled.identity.content_sha256 == original["content_sha256"]
    context = store.check_context(value["rules"][0]["check_id"])
    for relation in context["relations"]:
        if relation["predicate"] == "APPLIES_TO_FEATURE":
            assert relation["object"]["properties_json"] == {"feature_kind": "wall"}
    assert value == original


@pytest.mark.parametrize("field", ["dimension", "canonical_unit"])
def test_current_schema4_still_rejects_blank_geometric_metadata(field):
    value = grouped_payload()
    geometric = next(item for item in value["concepts"] if item["concept_type"] == "geometric")
    geometric["properties_json"][field] = ""
    with pytest.raises(DFMError) as exc_info:
        LocalOntologyStore.from_package(rehash(value))
    assert exc_info.value.code == "ontology_snapshot_invalid"


@pytest.mark.parametrize("unit,outcome", [("mm", "pass"), ("degree", "indeterminate"), ("", "indeterminate")])
def test_legacy_schema4_uses_actual_measured_units_before_passing(unit, outcome):
    store = LocalOntologyStore.from_package(legacy_schema4_payload())
    store.configure_geometric_bindings(wall_geometric_capability())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    evaluations, provenance = EvaluationEngine().evaluate([measurement(1.5, unit)], plan(compiled))
    assert evaluations[0].outcome == outcome
    if outcome == "indeterminate":
        if unit:
            assert all(item["error_code"] == "evaluation_unit_invalid" for item in evaluations[0].criterion_results)
        else:
            assert provenance[evaluations[0].evaluation_id]["unavailable_operands"]["actual"]["code"] == "evaluation_unit_invalid"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("unit", [None, "", "  "])
def test_ratio_cannot_treat_missing_measured_units_as_dimensionless(legacy, unit):
    value = legacy_schema4_payload() if legacy else grouped_payload()
    value["rules"][0]["acceptance_criteria_json"] = [[{
        "criterion_id": "self_ratio",
        "expression": {"op": "divide", "args": [{"operand": "actual"}, {"operand": "actual"}]},
        "comparator": "EQ",
        "threshold": 1.0,
        "result_unit": "ratio",
    }]]
    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings(wall_geometric_capability())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    evaluations, provenance = EvaluationEngine().evaluate([measurement(1.5, unit)], plan(compiled))
    assert evaluations[0].outcome == "indeterminate"
    assert provenance[evaluations[0].evaluation_id]["unavailable_operands"]["actual"]["code"] == "evaluation_unit_invalid"


def test_legacy_schema4_unavailable_result_does_not_require_unit():
    store = LocalOntologyStore.from_package(legacy_schema4_payload())
    store.configure_geometric_bindings(wall_geometric_capability())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    unavailable = replace(measurement(None, None), status="unsupported", reason="not measured")
    unavailable.validate()
    evaluations, provenance = EvaluationEngine().evaluate([unavailable], plan(compiled))
    assert evaluations[0].outcome == "indeterminate"
    assert provenance[evaluations[0].evaluation_id]["unavailable_operands"]["actual"]["code"] == "evaluation_operand_unavailable"


def test_legacy_schema4_without_worker_capability_skips_execution():
    store = LocalOntologyStore.from_package(legacy_schema4_payload())
    store.configure_geometric_bindings({})
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    assert not compiled.rule_bindings
    assert compiled.skipped_checks[0].reason == "geometric_id_unsupported"
    assert not store.geometric_scope_specs("injection")


@pytest.mark.parametrize("published,worker", [("boss", "solid_boss"), ("hole", "drilled_hole")])
def test_legacy_schema4_does_not_narrow_general_feature_meaning(published, worker):
    store = LocalOntologyStore.from_package(legacy_schema4_payload(published))
    store.configure_geometric_bindings(wall_geometric_capability(worker))
    with pytest.raises(DFMError, match="disagree on feature kind"):
        store.compile("injection", {"material": "ABS"}, operations())
    with pytest.raises(DFMError, match="disagree on feature kind"):
        store.geometric_scope_specs("injection")


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
    feature_id = "feature-main-wall-1"
    bindings = [
        replace(
            binding,
            feature_refs=[feature_id],
            additional_operands=[
                replace(operand, feature_refs=[feature_id])
                for operand in binding.additional_operands
            ],
        )
        for binding in compiled.rule_bindings
    ]
    geometric_ids = sorted({
        operand.geometric_id
        for binding in bindings
        for operand in binding.measurement_operands()
    })
    fragment = PlanFragmentRecord(
        fragment_id="fragment.catalog",
        snapshot_id="snapshot_catalog",
        source_discovery_snapshot_id="discovery.catalog",
        capability_version="2.0.0:test",
        dependency_fingerprint="f" * 64,
        confirmed_fact_refs=[],
        rule_binding_refs=[binding.binding_id for binding in bindings],
        items=[GeometricRequest(feature_id, geometric_ids, {})],
    )
    return PlanRecord(
        plan_id="catalog.plan",
        input_mode="step",
        analyzer_keys=["occt_cpp"],
        status="ready",
        created_at="2026-09-17T00:00:00Z",
        process="injection",
        scope_id=compiled.identity.scope_id,
        scope_version=compiled.identity.scope_version,
        rules=compiled.rules,
        rule_bindings=bindings,
        operations=[],
        ontology_snapshot_id=compiled.identity.snapshot_id,
        ontology_snapshot_sha256=compiled.identity.content_sha256,
        plan_fragments=[fragment],
    )


def measurement(value, unit="mm"):
    return GeometricResultRecord(
        result_id="result.wall",
        snapshot_id="snapshot_catalog",
        feature_id="feature-main-wall-1",
        geometric_id="G_WALL_THK_MIN",
        status="ok",
        value=value,
        unit=unit,
        context={},
        evidence={"geometry_refs": [], "field_refs": []},
        quality={},
        reason=None,
        provenance={"plan_fragment_ref": "fragment.catalog"},
        input_sha256="a" * 64,
    )


def test_agent_preserves_schema_2_contract_during_rollout():
    agent = json.loads(
        (ROOT / "tools/dfm/schemas/ontology_snapshot.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(agent).validate(payload())


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
    "actual,outcome,failed",
    [
        (1.5, "pass", []),
        (1.0, "fail", ["wall_minimum"]),
        (2.5, "fail", ["wall_maximum"]),
    ],
)
def test_schema_3_conjunction_compiles_and_evaluates_once(actual, outcome, failed):
    value = composite_payload()
    store = LocalOntologyStore.from_package(value)
    context_rule = store.check_context("check.main_wall_minimum_thickness")["rules"][0]
    assert "acceptance_criteria_json" in context_rule
    assert "expression" not in context_rule
    context = store.check_context("check.main_wall_minimum_thickness")
    assert "properties_json" in context["check"]
    operand_context = next(
        row for row in context["relations"] if row["predicate"] == "USES_OPERAND"
    )
    assert "qualifiers_json" in operand_context
    assert "properties_json" in operand_context["object"]
    assert "value_json" in context["factor_options"][0]
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    pinned = plan(compiled)
    binding_payload = pinned.rule_bindings[0].to_dict()
    assert "acceptance_criteria_json" in binding_payload
    assert "acceptance_criteria" not in binding_payload
    assert PlanRecord.from_dict(pinned.to_dict()).rule_bindings == pinned.rule_bindings
    evaluations, _ = EvaluationEngine().evaluate([measurement(actual)], pinned)
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation.outcome == outcome
    assert evaluation.severity == "medium"
    assert evaluation.severity_rationale == value["rules"][0]["severity_rationale"]
    assert [
        item["criterion_id"]
        for item in evaluation.criterion_results
        if item["outcome"] == "fail"
    ] == failed
    assert len(evaluation.criterion_results) == 2


def test_schema_3_missing_measurement_is_not_a_pass():
    store = LocalOntologyStore.from_package(composite_payload())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    evaluations, _ = EvaluationEngine().evaluate([], plan(compiled))
    assert len(evaluations) == 1
    assert evaluations[0].outcome == "indeterminate"
    assert all(
        item["outcome"] == "indeterminate" for item in evaluations[0].criterion_results
    )


def test_schema_3_rule_hash_is_independent_of_the_failed_criterion():
    store = LocalOntologyStore.from_package(composite_payload())
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    pinned = plan(compiled)
    hashes = {
        EvaluationEngine().evaluate([measurement(actual)], pinned)[0][0].rule_hash
        for actual in (1.0, 1.5, 2.5)
    }
    assert len(hashes) == 1


@pytest.mark.parametrize(
    "actual,outcome",
    [(1.5, "pass"), (2.5, "fail"), (3.5, "pass")],
)
def test_schema_4_or_of_and_groups_install_compile_and_evaluate(actual, outcome):
    value = grouped_payload()
    store = LocalOntologyStore.from_package(value)
    compiled = store.compile("injection", {"material": "ABS"}, operations())
    binding = compiled.rule_bindings[0]

    assert (
        binding.acceptance_criteria_json
        == value["rules"][0]["acceptance_criteria_json"]
    )
    evaluations, _ = EvaluationEngine().evaluate([measurement(actual)], plan(compiled))

    assert evaluations[0].outcome == outcome
    assert {item["group_index"] for item in evaluations[0].criterion_results} == {0, 1}


def test_schema_3_rejects_schema_4_grouped_criteria():
    value = grouped_payload()
    value["schema_version"] = 3

    with pytest.raises(DFMError) as exc_info:
        LocalOntologyStore.from_package(rehash(value))

    assert exc_info.value.code == "ontology_snapshot_invalid"


def test_schema_3_compiler_collects_operands_from_every_criterion():
    value = composite_payload()
    original = next(
        relation
        for relation in value["relations"]
        if relation["predicate"] == "USES_OPERAND"
        and relation["subject_id"] == "check.main_wall_minimum_thickness"
    )
    adjacent = deepcopy(original)
    adjacent["relation_id"] = "rel.check.wall.operand.adjacent"
    adjacent["sort_order"] = original["sort_order"] + 1
    adjacent["qualifiers_json"]["alias"] = "adjacent_main_wall_thickness"
    adjacent["qualifiers_json"]["operand_text"] = "相邻主体壁厚度"
    value["relations"].append(adjacent)
    value["rules"][0]["acceptance_criteria_json"][1]["expression"] = {
        "op": "divide",
        "args": [
            {"operand": "actual"},
            {"operand": "adjacent_main_wall_thickness"},
        ],
    }
    value["rules"][0]["acceptance_criteria_json"][1]["result_unit"] = "ratio"
    compiled = LocalOntologyStore.from_package(rehash(value)).compile(
        "injection", {"material": "ABS"}, operations()
    )
    assert len(compiled.rule_bindings) == 1
    binding = compiled.rule_bindings[0]
    assert {operand.alias for operand in binding.measurement_operands()} == {
        "actual",
        "adjacent_main_wall_thickness",
    }
    assert binding.acceptance_criteria_json[1]["expression"]["op"] == "divide"


@pytest.mark.parametrize(
    "change",
    [
        "geometric_condition",
        "duplicate_criterion",
        "legacy_expression",
        "legacy_json_field",
        "legacy_concept_field",
        "legacy_relation_field",
        "legacy_option_field",
        "warning_severity",
        "unit_mismatch",
    ],
)
def test_schema_3_rejects_invalid_new_rule_contract(change):
    value = composite_payload()
    rule = value["rules"][0]
    if change == "geometric_condition":
        rule["conditions_json"].append({
            "geometric_id": "actual",
            "operator": "LT",
            "value": 5,
            "unit": "mm",
        })
    elif change == "duplicate_criterion":
        rule["acceptance_criteria_json"][1]["criterion_id"] = "wall_minimum"
    elif change == "legacy_expression":
        rule["expression"] = {"operand": "actual"}
    elif change == "legacy_json_field":
        rule["acceptance_criteria"] = rule.pop("acceptance_criteria_json")
    elif change == "legacy_concept_field":
        concept = value["concepts"][0]
        concept["properties"] = concept.pop("properties_json")
    elif change == "legacy_relation_field":
        relation = value["relations"][0]
        relation["qualifiers"] = relation.pop("qualifiers_json")
    elif change == "legacy_option_field":
        option = value["factor_options"][0]
        option["value"] = option.pop("value_json")
    elif change == "unit_mismatch":
        rule["acceptance_criteria_json"][0]["result_unit"] = "degree"
    else:
        rule["severity"] = "warning"
    with pytest.raises(DFMError) as exc_info:
        LocalOntologyStore.from_package(rehash(value))
    assert exc_info.value.code == "ontology_snapshot_invalid"


def test_schema_3_skips_geometric_ids_missing_from_worker_capability():
    value = composite_payload()
    renamed_ids = {}
    for concept in value["concepts"]:
        if concept["concept_type"] != "geometric":
            continue
        previous_id = concept["concept_id"]
        concept["concept_id"] = f"G_UNIMPLEMENTED_{len(renamed_ids) + 1}"
        renamed_ids[previous_id] = concept["concept_id"]
    for relation in value["relations"]:
        relation["subject_id"] = renamed_ids.get(
            relation["subject_id"], relation["subject_id"]
        )
        relation["object_id"] = renamed_ids.get(
            relation["object_id"], relation["object_id"]
        )

    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings({})

    result = store.compile("injection", {"material": "ABS"}, operations())
    assert len(result.skipped_checks) > 0
    assert any(
        sc.reason == "geometric_id_unsupported" for sc in result.skipped_checks
    )
    assert [item["name"] for item in store.fact_requirements("injection")] == [
        "model_units"
    ]


def test_geometric_id_capability_binding_keeps_only_public_g_identity():
    value = composite_payload()
    operand_relation = next(
        row for row in value["relations"] if row["predicate"] == "USES_OPERAND"
    )
    previous_id = operand_relation["object_id"]
    for concept in value["concepts"]:
        if concept["concept_id"] != previous_id:
            continue
        concept["concept_id"] = "G_WALL_THK_MIN"
    for relation in value["relations"]:
        if relation["subject_id"] == previous_id:
            relation["subject_id"] = "G_WALL_THK_MIN"
        if relation["object_id"] == previous_id:
            relation["object_id"] = "G_WALL_THK_MIN"

    store = LocalOntologyStore.from_package(rehash(value))
    store.configure_geometric_bindings({
        "G_WALL_THK_MIN": {
            "feature_kind": "main_wall",
            "scope_modes": ["feature"],
            "direction_modes": ["none"],
            "requires_complete_inventory": [],
            "requires_relationships": [],
            "context_schema": {"type": "object", "additionalProperties": False},
        }
    })

    compiled = store.compile("injection", {"material": "ABS"}, operations())

    assert not compiled.skipped_checks
    binding = compiled.rule_bindings[0]
    assert binding.geometric_id == "G_WALL_THK_MIN"
    assert not hasattr(binding, "operation_id")
    assert not hasattr(binding, "metric_id")
    assert not hasattr(binding, "quantity_id")
    assert not hasattr(binding, "region_refs")
    assert binding.measurement_operands()[0].geometric_id == "G_WALL_THK_MIN"
    target = store.geometric_scope_specs("injection")[0]
    assert target["geometric_id"] == "G_WALL_THK_MIN"
    assert target["feature_kind"] == "main_wall"
    assert not {
        "worker_geometric_id",
        "operation_id",
        "calculator_id",
        "metric_id",
        "quantity_id",
        "region_role",
    }.intersection(target)


def test_schema_3_accepts_empty_unit_for_a_dimensionless_constant():
    value = composite_payload()
    criterion = value["rules"][0]["acceptance_criteria_json"][0]
    criterion.update({
        "expression": {
            "op": "multiply",
            "args": [
                {
                    "op": "divide",
                    "args": [
                        {"operand": "actual"},
                        {"operand": "actual"},
                    ],
                },
                {"constant": 100, "unit": ""},
            ],
        },
        "comparator": "GT",
        "threshold": 20,
        "result_unit": "percent",
    })

    compiled = LocalOntologyStore.from_package(rehash(value)).compile(
        "injection", {"material": "ABS"}, operations()
    )

    assert compiled.rule_bindings[0].acceptance_criteria_json[0] == criterion


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
        geometric["properties"]["worker_metric_id"] = "legacy.metric"
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
    assert evaluations[0].operand_values["actual"]["measurement_ids"] == ["result.wall"]
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
    for binding in plan(compiled).rule_bindings:
        Draft202012Validator(schema).validate(binding.to_dict())


def test_condition_only_operand_does_not_replace_expression_anchor():
    value = conditional_payload()
    geometric = deepcopy(
        next(item for item in value["concepts"] if item["concept_type"] == "geometric")
    )
    geometric["concept_id"] = "G_GATE"
    value["concepts"].append(geometric)
    relation = deepcopy(
        next(item for item in value["relations"] if item["predicate"] == "USES_OPERAND")
    )
    relation.update(relation_id="relation.gate", object_id="G_GATE", sort_order=-1)
    relation["qualifiers"]["alias"] = "gate"
    value["relations"].append(relation)
    value["rules"][0]["conditions"][1]["geometric_id"] = "gate"
    compiled = LocalOntologyStore.from_package(rehash(value)).compile(
        "injection", {"material": "ABS"}, operations()
    )
    assert all(binding.operand_alias == "actual" for binding in compiled.rule_bindings)
    pinned = plan(compiled)
    pinned = PlanRecord.from_dict(pinned.to_dict())
    gate = replace(
        measurement(3),
        result_id="result.gate",
        geometric_id="G_GATE",
    )
    evaluations, _ = EvaluationEngine().evaluate([measurement(1), gate], pinned)
    assert [item.rule_id for item in evaluations] == [value["rules"][0]["rule_id"]]
    assert set(evaluations[0].measurement_ids) == {"result.wall", "result.gate"}


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
                result_id=feature + ".result",
                feature_id=feature,
            )
        )
    pinned = plan(compiled)
    fragments = [
        replace(
            pinned.plan_fragments[0],
            fragment_id=f"fragment.catalog.{feature}",
            rule_binding_refs=[
                binding.binding_id
                for binding in bindings
                if feature in binding.feature_refs
            ],
            items=[
                replace(item, feature_id=feature)
                for item in pinned.plan_fragments[0].items
            ],
        )
        for feature in ("feature.a", "feature.b")
    ]
    measurements = [
        replace(
            item,
            provenance={
                "plan_fragment_ref": f"fragment.catalog.{item.feature_id}"
            },
        )
        for item in measurements
    ]
    evaluations, _ = EvaluationEngine().evaluate(
        measurements,
        replace(pinned, rule_bindings=bindings, plan_fragments=fragments),
    )
    assert {item.feature_refs[0]: item.rule_id for item in evaluations} == {
        "feature.a": "R_INJ_MAIN_WALL_MIN_ABS",
        "feature.b": "R_FALLBACK",
    }


def test_bundled_catalog_contains_only_public_geometric_identities():
    serialized = PACKAGE.read_text(encoding="utf-8")
    for forbidden in (
        "worker_geometric_id",
        "operation_id",
        "calculator_id",
        "metric_id",
        "quantity_id",
        "region_role",
        "face_indices",
        "task_id",
    ):
        assert f'"{forbidden}"' not in serialized
