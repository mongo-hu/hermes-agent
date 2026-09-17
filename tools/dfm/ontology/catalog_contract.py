"""Current Mold publication contract; legacy packages are read by store.py."""

from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from ..contracts import _expression_operand_aliases
from ..errors import DFMError


PROPERTY_FIELDS = {
    "process": set(),
    "feature_type": {"worker_kind"},
    "geometric": {"worker_geometric_id", "quantity_id", "dimension", "canonical_unit"},
    "factor": {"runtime_key", "default_value", "question", "source_policy"},
    "check": {"default_severity", "report_group"},
}
ENDPOINTS = {
    "HAS_CHECK": ({"process"}, {"check"}),
    "APPLIES_TO_FEATURE": ({"check"}, {"feature_type"}),
    "USES_OPERAND": ({"check"}, {"geometric"}),
    "REQUIRES_FACTOR": ({"process", "check"}, {"factor"}),
    "AFFECTS": ({"factor", "feature_type"}, {"check"}),
    "RELATED_TO": (set(PROPERTY_FIELDS), set(PROPERTY_FIELDS)),
}


def current_catalog(payload: Mapping[str, Any]) -> bool:
    concepts = payload.get("concepts", [])
    if not isinstance(concepts, list) or not isinstance(
        payload.get("relations", []), list
    ):
        return False
    return (
        any(
            item.get("concept_type") == "geometric"
            for item in concepts
            if isinstance(item, Mapping)
        )
        or not any(
            item.get("concept_type") in {"metric", "region_type"}
            for item in concepts
            if isinstance(item, Mapping)
        )
        or any(
            "operand_text" in item.get("qualifiers", {})
            for item in payload.get("relations", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("qualifiers", {}), Mapping)
        )
    )


def finite(value: Any) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    except OverflowError:
        return False


def invalid(message: str, **details: Any) -> None:
    raise DFMError("ontology_snapshot_invalid", message, details)


def validate_catalog(payload: Mapping[str, Any], validate_source_policy) -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/ontology_snapshot.schema.json"
        ).read_text(encoding="utf-8")
    )
    error = next(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            payload
        ),
        None,
    )
    if error:
        invalid(
            "The publication does not satisfy the Mold catalog contract.",
            path=list(error.path),
            reason=error.message,
        )
    concepts = {item["concept_id"]: item for item in payload["concepts"]}
    if len(concepts) != len(payload["concepts"]):
        invalid("Concept identities must be unique.")
    process_id = f"process.{payload['rule_set']['process']}"
    if concepts.get(process_id, {}).get("concept_type") != "process":
        invalid("The publication does not declare its process concept.")
    for concept_id, concept in concepts.items():
        kind = concept["concept_type"]
        properties = concept.get("properties", {})
        if not isinstance(properties, dict) or set(properties) != PROPERTY_FIELDS[kind]:
            invalid(
                "Concept properties must match the documented whitelist.",
                concept_id=concept_id,
            )
        bindings = {
            "feature_type": ("worker_kind",),
            "geometric": ("worker_geometric_id", "quantity_id", "dimension"),
            "factor": ("runtime_key",),
        }
        for name in bindings.get(kind, ()):
            if not isinstance(properties[name], str) or not properties[name].strip():
                invalid(
                    "An executable Concept binding is unresolved.",
                    concept_id=concept_id,
                    field=name,
                )
        if kind == "geometric" and not isinstance(properties["canonical_unit"], str):
            invalid("Geometric canonical_unit must be a string.", concept_id=concept_id)
        if kind == "factor":
            validate_source_policy(concept_id, properties["source_policy"])
            policy = properties["source_policy"]
            if (
                set(policy["auto_accept_sources"])
                | set(policy["confirmation_required_sources"])
            ) & {"DWG", "GEO"} and policy["min_confidence"] is None:
                invalid(
                    "Recognition sources require a confidence threshold.",
                    concept_id=concept_id,
                )
    relations = payload["relations"]
    if len({item["relation_id"] for item in relations}) != len(relations):
        invalid("Relation identities must be unique.")
    operands = defaultdict(dict)
    features = defaultdict(set)
    checks = set()
    for relation in relations:
        subject, target = relation["subject_id"], relation["object_id"]
        kinds = ENDPOINTS[relation["predicate"]]
        if (
            concepts.get(subject, {}).get("concept_type") not in kinds[0]
            or concepts.get(target, {}).get("concept_type") not in kinds[1]
        ):
            invalid(
                "A relation has missing or incompatible endpoints.",
                relation_id=relation["relation_id"],
            )
        if relation["predicate"] == "USES_OPERAND":
            qualifier = relation["qualifiers"]
            alias = qualifier["alias"]
            if (
                not alias.strip()
                or alias in operands[subject]
                or not qualifier["operand_text"].strip()
            ):
                invalid(
                    "Operand aliases must be unique and comparison text nonempty.",
                    check_id=subject,
                )
            if qualifier["aggregation"] not in {
                "minimum",
                "maximum",
                "mean",
                "median",
                "sum",
                "count",
                "identity",
            }:
                invalid("Unsupported Operand aggregation.", check_id=subject)
            operands[subject][alias] = concepts[target]
        elif relation["predicate"] == "APPLIES_TO_FEATURE":
            features[subject].add(target)
        elif relation["predicate"] == "HAS_CHECK" and subject == process_id:
            checks.add(target)
    for check in operands:
        if not features[check]:
            invalid("Every executable Check must reference a Feature.", check_id=check)
    versions = set()
    for rule in payload["rules"]:
        check = rule["check_id"]
        if (
            rule["rule_version_id"] in versions
            or check not in checks
            or not operands[check]
        ):
            invalid(
                "Rule identity or Process/Check/Operand reference is invalid.",
                rule_id=rule["rule_id"],
            )
        versions.add(rule["rule_version_id"])
        aliases = _expression_operand_aliases(
            rule["expression"], binding_id=rule["rule_id"]
        )
        if not aliases or not aliases.issubset(operands[check]):
            invalid(
                "Expression references undeclared Operand aliases.",
                rule_id=rule["rule_id"],
            )
        for condition in rule["conditions"]:
            if "geometric_id" in condition:
                geometric = operands[check].get(condition["geometric_id"])
                if (
                    geometric is None
                    or condition["unit"] != geometric["properties"]["canonical_unit"]
                    or not finite(condition["value"])
                ):
                    invalid(
                        "Geometric conditions require a local Operand alias, finite value and canonical unit.",
                        rule_id=rule["rule_id"],
                    )
            elif (
                concepts.get(condition["factor_id"], {}).get("concept_type") != "factor"
            ):
                invalid(
                    "Factor condition references an invalid Factor.",
                    rule_id=rule["rule_id"],
                )
    for option in payload["factor_options"]:
        if concepts.get(option["factor_id"], {}).get("concept_type") != "factor":
            invalid("Factor option references an invalid Factor.")
