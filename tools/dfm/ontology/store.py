"""Read-only runtime projection of a published DFM ontology and rule set.

The management service owns authoring, approval, tenant inheritance, and
publication.  Hermes consumes one immutable, flattened publication through a
small SQLite database.  Runtime code never mutates individual concepts or
rules; replacing the complete publication is the only write operation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

from ..contracts import EffectiveRule, PlanOperation, RuleBinding, RuleOperand
from ..errors import DFMError


ONTOLOGY_SNAPSHOT_SCHEMA_VERSION = 2
SUPPORTED_ONTOLOGY_SNAPSHOT_SCHEMA_VERSIONS = {1, 2}
LOCAL_DATABASE_SCHEMA_VERSION = 1
_CONCEPT_TYPES = {
    "process",
    "feature_type",
    "region_type",
    "metric",
    "check",
    "factor",
}
_PREDICATES = {
    "HAS_CHECK",
    "HAS_REGION",
    "APPLIES_TO_FEATURE",
    "APPLIES_TO_REGION",
    "USES_OPERAND",
    "REQUIRES_FACTOR",
    "AFFECTS",
    "RELATED_TO",
}
_RELATION_ENDPOINT_TYPES = {
    "HAS_CHECK": ({"process"}, {"check"}),
    "HAS_REGION": ({"feature_type"}, {"region_type"}),
    "APPLIES_TO_FEATURE": ({"check"}, {"feature_type"}),
    "APPLIES_TO_REGION": ({"check"}, {"region_type"}),
    "USES_OPERAND": ({"check"}, {"metric"}),
    "REQUIRES_FACTOR": ({"process", "check"}, {"factor"}),
}
_LEGACY_OPERAND_SELECTOR_KEYS = {
    "worker_metric_id",
    "quantity_id",
    "feature_type_id",
    "region_type_id",
    "feature_kind",
    "region_role",
}
_CONDITION_OPERATORS = {"EQ", "IN", "GT", "GTE", "LT", "LTE", "BETWEEN", "EXISTS"}
_COMPARATORS = {
    "GT": ">",
    "GTE": ">=",
    "LT": "<",
    "LTE": "<=",
    "EQ": "==",
    "NE": "!=",
    "BETWEEN": "between",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "==": "==",
    "!=": "!=",
    "between": "between",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(payload: Mapping[str, Any]) -> str:
    normalized = dict(payload)
    normalized.pop("content_sha256", None)
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


@dataclass(frozen=True)
class OntologySnapshotIdentity:
    snapshot_id: str
    ontology_version: str
    rule_set_code: str
    rule_set_version: str
    process: str
    scope_type: str
    scope_key: str
    content_sha256: str
    published_at: str

    @property
    def scope_id(self) -> str:
        return self.rule_set_code

    @property
    def scope_version(self) -> str:
        return self.rule_set_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "ontology_version": self.ontology_version,
            "rule_set_code": self.rule_set_code,
            "rule_set_version": self.rule_set_version,
            "process": self.process,
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "content_sha256": self.content_sha256,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class CompiledOntologyPlan:
    identity: OntologySnapshotIdentity
    rules: dict[str, EffectiveRule]
    rule_bindings: list[RuleBinding]
    binding_selectors: dict[str, dict[str, dict[str, str]]]
    accepted_fact_names: set[str]


class LocalOntologyStore:
    """Install and query one flattened, immutable ontology publication."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._memory: sqlite3.Connection | None = None
        if self.path is None:
            self._memory = sqlite3.connect(":memory:")
            self._memory.row_factory = sqlite3.Row

    @classmethod
    def from_package(
        cls,
        package_path: Path,
        *,
        database_path: Path | None = None,
    ) -> "LocalOntologyStore":
        store = cls(database_path)
        store.install_package(package_path)
        return store

    def close(self) -> None:
        if self._memory is not None:
            self._memory.close()
            self._memory = None

    def ensure_bootstrap(self, package_path: Path) -> None:
        if self.path is None:
            if not self._has_schema(self._memory):
                self.install_package(package_path)
            return
        if not self.path.is_file():
            self.install_package(package_path)
            return
        try:
            self.identity()
        except DFMError:
            raise

    def install_package(self, package: Path | Mapping[str, Any]) -> None:
        payload = self._read_package(package)
        self._validate_package(payload)
        digest = _content_hash(payload)
        declared = str(payload.get("content_sha256") or "")
        if declared and declared != digest:
            raise DFMError(
                "ontology_snapshot_invalid",
                "The DFM ontology publication hash does not match its content.",
                {"declared": declared, "actual": digest},
            )

        if self.path is None:
            assert self._memory is not None
            self._rebuild(self._memory, payload, digest)
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        try:
            self._rebuild(connection, payload, digest)
            connection.close()
            os.replace(temporary, self.path)
        except Exception:
            connection.close()
            temporary.unlink(missing_ok=True)
            raise

    def identity(self) -> OntologySnapshotIdentity:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshot_metadata WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            raise DFMError(
                "ontology_snapshot_missing",
                "No published DFM ontology snapshot is installed.",
            )
        return OntologySnapshotIdentity(
            snapshot_id=row["snapshot_id"],
            ontology_version=row["ontology_version"],
            rule_set_code=row["rule_set_code"],
            rule_set_version=row["rule_set_version"],
            process=row["process"],
            scope_type=row["scope_type"],
            scope_key=row["scope_key"],
            content_sha256=row["content_sha256"],
            published_at=row["published_at"],
        )

    def fact_requirements(self, process: str) -> tuple[dict[str, Any], ...]:
        identity = self.identity()
        if identity.process != process:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT factor.concept_id, factor.properties_json, relation.qualifiers_json
                FROM ontology_relation AS relation
                JOIN ontology_concept AS subject ON subject.concept_id = relation.subject_id
                JOIN ontology_concept AS factor ON factor.concept_id = relation.object_id
                WHERE relation.predicate = 'REQUIRES_FACTOR'
                  AND subject.concept_type IN ('process', 'check')
                  AND factor.concept_type = 'factor'
                ORDER BY relation.sort_order, relation.relation_id
                """
            ).fetchall()
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            properties = _load_json(row["properties_json"], {})
            qualifiers = _load_json(row["qualifiers_json"], {})
            runtime_key = str(properties.get("runtime_key") or row["concept_id"])
            requirement = {
                "name": runtime_key,
                "question": str(
                    qualifiers.get("question")
                    or properties.get("question")
                    or f"Please provide {runtime_key}."
                ),
                "phase": str(qualifiers.get("phase") or "analysis"),
                "required_by": sorted({
                    str(item) for item in qualifiers.get("required_by", []) if str(item)
                }),
            }
            current = merged.get(runtime_key)
            if current is None:
                merged[runtime_key] = requirement
                continue
            current["required_by"] = sorted(
                set(current["required_by"]) | set(requirement["required_by"])
            )
            if current["phase"] != "discovery" and requirement["phase"] == "discovery":
                current["phase"] = "discovery"
                current["question"] = requirement["question"]
        return tuple(merged[key] for key in sorted(merged))

    def check_context(self, check_id: str) -> dict[str, Any]:
        """Return the bounded semantic context supplied to planners or an LLM."""

        with self._connect() as connection:
            check = connection.execute(
                "SELECT * FROM ontology_concept WHERE concept_id = ? AND concept_type = 'check'",
                (check_id,),
            ).fetchone()
            if check is None:
                raise DFMError(
                    "ontology_check_missing",
                    "The requested DFM Check is absent from the installed ontology.",
                    {"check_id": check_id},
                )
            relations = connection.execute(
                """
                SELECT relation.*, object.concept_type AS object_type,
                       object.name_zh AS object_name_zh,
                       object.definition AS object_definition,
                       object.data_schema_json AS object_data_schema_json,
                       object.properties_json AS object_properties_json
                FROM ontology_relation AS relation
                JOIN ontology_concept AS object ON object.concept_id = relation.object_id
                WHERE relation.subject_id = ?
                ORDER BY relation.sort_order, relation.relation_id
                """,
                (check_id,),
            ).fetchall()
            rules = connection.execute(
                "SELECT * FROM rule_version WHERE check_id = ? ORDER BY priority DESC, rule_id",
                (check_id,),
            ).fetchall()
            factor_ids = [
                row["object_id"]
                for row in relations
                if row["predicate"] == "REQUIRES_FACTOR"
            ]
            options: list[sqlite3.Row] = []
            if factor_ids:
                placeholders = ",".join("?" for _ in factor_ids)
                options = connection.execute(
                    f"SELECT * FROM factor_option WHERE factor_id IN ({placeholders}) "
                    "ORDER BY factor_id, sort_order, option_code",
                    factor_ids,
                ).fetchall()
        return {
            "snapshot": self.identity().to_dict(),
            "check": self._concept_payload(check),
            "relations": [self._relation_context_payload(row) for row in relations],
            "factor_options": [self._factor_option_payload(row) for row in options],
            "rules": [self._rule_payload(row) for row in rules],
        }

    def check_ids(self, process: str) -> tuple[str, ...]:
        identity = self.identity()
        if identity.process != process:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT check_concept.concept_id
                FROM ontology_relation AS relation
                JOIN ontology_concept AS check_concept
                  ON check_concept.concept_id = relation.object_id
                WHERE relation.subject_id = ?
                  AND relation.predicate = 'HAS_CHECK'
                  AND check_concept.concept_type = 'check'
                  AND check_concept.status = 'active'
                ORDER BY relation.sort_order, check_concept.concept_id
                """,
                (f"process.{process}",),
            ).fetchall()
        return tuple(str(row["concept_id"]) for row in rows)

    def analysis_target_specs(self, process: str) -> tuple[dict[str, Any], ...]:
        """Return semantic Region/Metric targets used after feature discovery."""

        if self.identity().process != process:
            return ()
        check_ids = self.check_ids(process)
        if not check_ids:
            return ()
        placeholders = ",".join("?" for _ in check_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT relation.*, metric.properties_json AS metric_properties_json
                FROM ontology_relation AS relation
                JOIN ontology_concept AS metric
                  ON metric.concept_id = relation.object_id
                WHERE relation.predicate = 'USES_OPERAND'
                  AND relation.subject_id IN ({placeholders})
                ORDER BY relation.sort_order, relation.relation_id
                """,
                check_ids,
            ).fetchall()
            specs: dict[tuple[str, str, str], dict[str, Any]] = {}
            for row in rows:
                target = self._resolve_operand_target(
                    connection, str(row["subject_id"]), row
                )
                key = (
                    target["feature_kind"],
                    target["region_role"],
                    target["metric_id"],
                )
                item = specs.setdefault(
                    key,
                    {
                        "feature_kind": target["feature_kind"],
                        "region_role": target["region_role"],
                        "metrics": [],
                        "check_ids": [],
                        "status": "released",
                    },
                )
                if target["metric_id"] not in item["metrics"]:
                    item["metrics"].append(target["metric_id"])
                if row["subject_id"] not in item["check_ids"]:
                    item["check_ids"].append(row["subject_id"])
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in specs.values():
            key = (item["feature_kind"], item["region_role"])
            combined = grouped.setdefault(
                key,
                {
                    "feature_kind": item["feature_kind"],
                    "region_role": item["region_role"],
                    "metrics": [],
                    "check_ids": [],
                    "status": "released",
                },
            )
            combined["metrics"].extend(
                metric
                for metric in item["metrics"]
                if metric not in combined["metrics"]
            )
            combined["check_ids"].extend(
                check_id
                for check_id in item["check_ids"]
                if check_id not in combined["check_ids"]
            )
        return tuple(grouped[key] for key in sorted(grouped))

    def compile(
        self,
        process: str,
        facts: Mapping[str, Any],
        operations: Sequence[PlanOperation],
    ) -> CompiledOntologyPlan:
        """Compile released ontology rows into the existing generic rule contract."""

        identity = self.identity()
        if identity.process != process:
            raise DFMError(
                "ontology_scope_mismatch",
                "The installed ontology publication belongs to another process.",
                {"expected": process, "actual": identity.process},
            )
        provided_facts = self._normalize_facts(facts)
        requirements = self.fact_requirements(process)
        accepted = {item["name"] for item in requirements}
        with self._connect() as connection:
            factor_rows = connection.execute(
                "SELECT concept_id, properties_json FROM ontology_concept "
                "WHERE concept_type = 'factor' AND status = 'active'"
            ).fetchall()
            factor_runtime_keys: dict[str, str] = {}
            normalized_facts: dict[str, Any] = {}
            for factor in factor_rows:
                properties = _load_json(factor["properties_json"], {})
                concept_id = str(factor["concept_id"])
                runtime_key = str(properties.get("runtime_key") or concept_id)
                factor_runtime_keys[concept_id] = runtime_key
                if runtime_key in provided_facts:
                    normalized_facts[concept_id] = provided_facts[runtime_key]
                elif "default_value" in properties:
                    normalized_facts[concept_id] = properties["default_value"]
            checks = connection.execute(
                """
                SELECT check_concept.*
                FROM ontology_relation AS relation
                JOIN ontology_concept AS process_concept
                  ON process_concept.concept_id = relation.subject_id
                JOIN ontology_concept AS check_concept
                  ON check_concept.concept_id = relation.object_id
                WHERE process_concept.concept_id = ?
                  AND relation.predicate = 'HAS_CHECK'
                  AND check_concept.concept_type = 'check'
                  AND check_concept.status = 'active'
                ORDER BY relation.sort_order, check_concept.concept_id
                """,
                (f"process.{process}",),
            ).fetchall()
            compiled_rules: dict[str, EffectiveRule] = {}
            bindings: list[RuleBinding] = []
            selectors: dict[str, dict[str, dict[str, str]]] = {}
            for check in checks:
                check_id = str(check["concept_id"])
                rules = connection.execute(
                    "SELECT * FROM rule_version WHERE check_id = ? AND status = 'released'",
                    (check_id,),
                ).fetchall()
                selected = self._select_rule(rules, normalized_facts, check_id)
                if selected is None:
                    continue
                operand_rows = connection.execute(
                    """
                    SELECT relation.*, metric.properties_json AS metric_properties_json
                    FROM ontology_relation AS relation
                    JOIN ontology_concept AS metric ON metric.concept_id = relation.object_id
                    WHERE relation.subject_id = ? AND relation.predicate = 'USES_OPERAND'
                    ORDER BY relation.sort_order, relation.relation_id
                    """,
                    (check_id,),
                ).fetchall()
                binding, binding_selectors = self._compile_binding(
                    connection,
                    check_id,
                    selected,
                    operand_rows,
                    operations,
                    factor_runtime_keys,
                )
                rule_id = str(selected["rule_id"])
                compiled_rules[rule_id] = EffectiveRule(
                    value=_load_json(selected["threshold_json"], None),
                    unit=selected["result_unit"],
                    source=(
                        f"ontology:{identity.snapshot_id}/{rule_id}"
                        f"@{selected['version']}#{selected['content_sha256']}"
                    ),
                    version=str(selected["version"]),
                )
                bindings.append(binding)
                selectors[binding.binding_id] = binding_selectors
        return CompiledOntologyPlan(
            identity=identity,
            rules=compiled_rules,
            rule_bindings=bindings,
            binding_selectors=selectors,
            accepted_fact_names=accepted,
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            yield self._memory
            return
        if self.path is None or not self.path.is_file():
            raise DFMError(
                "ontology_snapshot_missing",
                "No published DFM ontology snapshot is installed.",
                {"path": str(self.path) if self.path else None},
            )
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except sqlite3.DatabaseError as exc:
            raise DFMError(
                "ontology_snapshot_invalid",
                "The local DFM ontology database cannot be queried.",
                {"path": str(self.path)},
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _has_schema(connection: sqlite3.Connection | None) -> bool:
        if connection is None:
            return False
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshot_metadata'"
        ).fetchone()
        return row is not None

    @staticmethod
    def _read_package(package: Path | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(package, Mapping):
            return dict(package)
        try:
            payload = json.loads(Path(package).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DFMError(
                "ontology_snapshot_invalid",
                "The DFM ontology publication could not be loaded.",
                {"path": str(package)},
            ) from exc
        if not isinstance(payload, dict):
            raise DFMError(
                "ontology_snapshot_invalid",
                "The DFM ontology publication root must be an object.",
            )
        return payload

    @classmethod
    def _validate_package(cls, payload: Mapping[str, Any]) -> None:
        required_strings = (
            "snapshot_id",
            "ontology_version",
            "published_at",
            "content_sha256",
        )
        if (
            payload.get("schema_version")
            not in SUPPORTED_ONTOLOGY_SNAPSHOT_SCHEMA_VERSIONS
            or any(
                not isinstance(payload.get(key), str) or not payload[key]
                for key in required_strings
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(payload.get("content_sha256") or "")
            )
        ):
            cls._invalid("The DFM ontology publication identity is invalid.")
        rule_set = payload.get("rule_set")
        if not isinstance(rule_set, Mapping) or any(
            not isinstance(rule_set.get(key), str) or not rule_set[key]
            for key in (
                "rule_set_code",
                "version",
                "process",
                "scope_type",
                "scope_key",
            )
        ):
            cls._invalid("The DFM ontology publication rule-set identity is invalid.")
        for key in ("concepts", "relations", "factor_options", "rules"):
            if not isinstance(payload.get(key), list):
                cls._invalid(f"The DFM ontology publication requires a {key} array.")

        concepts: dict[str, Mapping[str, Any]] = {}
        for item in payload["concepts"]:
            if not isinstance(item, Mapping):
                cls._invalid("Ontology concepts must be objects.")
            concept_id = str(item.get("concept_id") or "")
            concept_type = str(item.get("concept_type") or "")
            if (
                not concept_id
                or concept_id in concepts
                or concept_type not in _CONCEPT_TYPES
                or not str(item.get("name_zh") or "")
                or not str(item.get("definition") or "")
            ):
                cls._invalid(
                    "An ontology concept is invalid or duplicated.",
                    {"concept_id": concept_id},
                )
            concepts[concept_id] = item
        process_id = f"process.{rule_set['process']}"
        if concepts.get(process_id, {}).get("concept_type") != "process":
            cls._invalid("The publication does not declare its process concept.")

        relations: set[str] = set()
        relation_items: list[Mapping[str, Any]] = []
        check_ids = {
            concept_id
            for concept_id, item in concepts.items()
            if item["concept_type"] == "check"
        }
        factor_ids = {
            concept_id
            for concept_id, item in concepts.items()
            if item["concept_type"] == "factor"
        }
        for item in payload["relations"]:
            if not isinstance(item, Mapping):
                cls._invalid("Ontology relations must be objects.")
            relation_id = str(item.get("relation_id") or "")
            if (
                not relation_id
                or relation_id in relations
                or item.get("predicate") not in _PREDICATES
                or item.get("subject_id") not in concepts
                or item.get("object_id") not in concepts
                or not isinstance(item.get("qualifiers", {}), Mapping)
            ):
                cls._invalid(
                    "An ontology relation is invalid or duplicated.",
                    {"relation_id": relation_id},
                )
            relations.add(relation_id)
            relation_items.append(item)

        cls._validate_relation_graph(
            concepts,
            relation_items,
            schema_version=int(payload["schema_version"]),
        )

        for item in payload["factor_options"]:
            if (
                not isinstance(item, Mapping)
                or item.get("factor_id") not in factor_ids
                or not str(item.get("option_code") or "")
            ):
                cls._invalid("A factor option is invalid.")

        versions: set[str] = set()
        for item in payload["rules"]:
            if not isinstance(item, Mapping):
                cls._invalid("Rule versions must be objects.")
            version_id = str(item.get("rule_version_id") or "")
            if (
                not version_id
                or version_id in versions
                or item.get("check_id") not in check_ids
                or not str(item.get("rule_id") or "")
                or not str(item.get("version") or "")
                or item.get("comparator") not in _COMPARATORS
                or not isinstance(item.get("conditions", []), list)
                or not isinstance(item.get("expression"), Mapping)
                or item.get("status") != "released"
            ):
                cls._invalid(
                    "A released rule version is invalid.",
                    {"rule_version_id": version_id},
                )
            for condition in item["conditions"]:
                if (
                    not isinstance(condition, Mapping)
                    or condition.get("factor_id") not in factor_ids
                    or condition.get("operator") not in _CONDITION_OPERATORS
                ):
                    cls._invalid(
                        "A rule applicability condition is invalid.",
                        {"rule_version_id": version_id},
                    )
            versions.add(version_id)

    @classmethod
    def _validate_relation_graph(
        cls,
        concepts: Mapping[str, Mapping[str, Any]],
        relations: Sequence[Mapping[str, Any]],
        *,
        schema_version: int,
    ) -> None:
        """Validate executable relation semantics before installing a snapshot."""

        for relation in relations:
            predicate = str(relation["predicate"])
            endpoint_types = _RELATION_ENDPOINT_TYPES.get(predicate)
            if endpoint_types is not None:
                subject_type = str(
                    concepts[str(relation["subject_id"])]["concept_type"]
                )
                object_type = str(concepts[str(relation["object_id"])]["concept_type"])
                if (
                    subject_type not in endpoint_types[0]
                    or object_type not in endpoint_types[1]
                ):
                    cls._invalid(
                        "An ontology relation connects incompatible concept types.",
                        {
                            "relation_id": relation["relation_id"],
                            "predicate": predicate,
                            "subject_type": subject_type,
                            "object_type": object_type,
                        },
                    )

            qualifiers = relation.get("qualifiers", {})
            if predicate == "USES_OPERAND":
                alias = str(qualifiers.get("alias") or "")
                if not alias or not str(qualifiers.get("aggregation") or ""):
                    cls._invalid(
                        "A USES_OPERAND relation requires alias and aggregation.",
                        {"relation_id": relation["relation_id"]},
                    )
                if schema_version >= 2 and _LEGACY_OPERAND_SELECTOR_KEYS.intersection(
                    qualifiers
                ):
                    cls._invalid(
                        "Schema 2 operands must resolve Metric, Feature, and Region from concepts and relations.",
                        {"relation_id": relation["relation_id"]},
                    )
            elif predicate == "APPLIES_TO_REGION":
                aliases = qualifiers.get("operand_aliases")
                if aliases is not None and (
                    not isinstance(aliases, list)
                    or not aliases
                    or len({str(item) for item in aliases}) != len(aliases)
                    or any(not isinstance(item, str) or not item for item in aliases)
                ):
                    cls._invalid(
                        "APPLIES_TO_REGION.operand_aliases must contain unique non-empty aliases.",
                        {"relation_id": relation["relation_id"]},
                    )

        if schema_version < 2:
            return

        operands_by_check: dict[str, list[Mapping[str, Any]]] = {}
        regions_by_check: dict[str, list[Mapping[str, Any]]] = {}
        features_by_check: dict[str, set[str]] = {}
        parent_features_by_region: dict[str, set[str]] = {}
        for relation in relations:
            predicate = relation["predicate"]
            subject_id = str(relation["subject_id"])
            object_id = str(relation["object_id"])
            if predicate == "USES_OPERAND":
                operands_by_check.setdefault(subject_id, []).append(relation)
            elif predicate == "APPLIES_TO_REGION":
                regions_by_check.setdefault(subject_id, []).append(relation)
            elif predicate == "APPLIES_TO_FEATURE":
                features_by_check.setdefault(subject_id, set()).add(object_id)
            elif predicate == "HAS_REGION":
                parent_features_by_region.setdefault(object_id, set()).add(subject_id)

        for check_id, operands in operands_by_check.items():
            aliases = [str(item["qualifiers"]["alias"]) for item in operands]
            if len(set(aliases)) != len(aliases):
                cls._invalid(
                    "Operand aliases must be unique inside one Check.",
                    {"check_id": check_id},
                )
            for operand in operands:
                alias = str(operand["qualifiers"]["alias"])
                metric = concepts[str(operand["object_id"])]
                metric_properties = metric.get("properties", {})
                if not str(metric_properties.get("worker_metric_id") or "") or not str(
                    metric_properties.get("quantity_id") or ""
                ):
                    cls._invalid(
                        "An operand Metric must declare worker_metric_id and quantity_id.",
                        {"check_id": check_id, "operand_alias": alias},
                    )
                target_region = cls._select_region_relation_payload(
                    check_id,
                    alias,
                    regions_by_check.get(check_id, []),
                )
                region_id = str(target_region["object_id"])
                applicable_parents = parent_features_by_region.get(
                    region_id, set()
                ).intersection(features_by_check.get(check_id, set()))
                if len(applicable_parents) != 1:
                    cls._invalid(
                        "An operand Region must belong to exactly one Feature applicable to its Check.",
                        {
                            "check_id": check_id,
                            "operand_alias": alias,
                            "region_type_id": region_id,
                            "matching_feature_type_ids": sorted(applicable_parents),
                        },
                    )
                feature_id = next(iter(applicable_parents))
                feature_properties = concepts[feature_id].get("properties", {})
                region_properties = concepts[region_id].get("properties", {})
                if not str(feature_properties.get("worker_kind") or "") or not str(
                    region_properties.get("worker_role") or ""
                ):
                    cls._invalid(
                        "Executable Feature and Region concepts require worker_kind and worker_role.",
                        {"check_id": check_id, "operand_alias": alias},
                    )

    @classmethod
    def _select_region_relation_payload(
        cls,
        check_id: str,
        alias: str,
        relations: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        specific = [
            item
            for item in relations
            if alias in item.get("qualifiers", {}).get("operand_aliases", [])
        ]
        general = [
            item
            for item in relations
            if "operand_aliases" not in item.get("qualifiers", {})
        ]
        matches = specific if specific else general
        if len(matches) != 1:
            cls._invalid(
                "Each Check operand must resolve to exactly one APPLIES_TO_REGION relation.",
                {
                    "check_id": check_id,
                    "operand_alias": alias,
                    "matching_relation_ids": [item["relation_id"] for item in matches],
                },
            )
        return matches[0]

    @classmethod
    def _rebuild(
        cls,
        connection: sqlite3.Connection,
        payload: Mapping[str, Any],
        digest: str,
    ) -> None:
        connection.executescript(
            """
            PRAGMA foreign_keys = OFF;
            DROP TABLE IF EXISTS factor_option;
            DROP TABLE IF EXISTS ontology_relation;
            DROP TABLE IF EXISTS rule_version;
            DROP TABLE IF EXISTS ontology_concept;
            DROP TABLE IF EXISTS snapshot_metadata;
            PRAGMA foreign_keys = ON;

            CREATE TABLE snapshot_metadata (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                database_schema_version INTEGER NOT NULL,
                snapshot_id TEXT NOT NULL UNIQUE,
                ontology_version TEXT NOT NULL,
                rule_set_code TEXT NOT NULL,
                rule_set_version TEXT NOT NULL,
                process TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                published_at TEXT NOT NULL,
                installed_at TEXT NOT NULL
            );

            CREATE TABLE ontology_concept (
                concept_id TEXT PRIMARY KEY,
                concept_type TEXT NOT NULL,
                name_zh TEXT NOT NULL,
                name_en TEXT,
                definition TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                data_schema_json TEXT,
                properties_json TEXT NOT NULL,
                status TEXT NOT NULL
            );

            CREATE TABLE ontology_relation (
                relation_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL REFERENCES ontology_concept(concept_id),
                predicate TEXT NOT NULL,
                object_id TEXT NOT NULL REFERENCES ontology_concept(concept_id),
                qualifiers_json TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(subject_id, predicate, object_id, qualifiers_json)
            );

            CREATE TABLE factor_option (
                factor_id TEXT NOT NULL REFERENCES ontology_concept(concept_id),
                option_code TEXT NOT NULL,
                name_zh TEXT NOT NULL,
                value_json TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                PRIMARY KEY(factor_id, option_code)
            );

            CREATE TABLE rule_version (
                rule_version_id TEXT PRIMARY KEY,
                rule_id TEXT NOT NULL,
                version TEXT NOT NULL,
                check_id TEXT NOT NULL REFERENCES ontology_concept(concept_id),
                name TEXT NOT NULL,
                conditions_json TEXT NOT NULL,
                expression_json TEXT NOT NULL,
                comparator TEXT NOT NULL,
                threshold_json TEXT NOT NULL,
                result_unit TEXT,
                severity TEXT NOT NULL,
                recommendation_template TEXT,
                explanation_text TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                citation_refs_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                UNIQUE(rule_id, version)
            );

            CREATE INDEX idx_concept_type ON ontology_concept(concept_type, status);
            CREATE INDEX idx_relation_subject ON ontology_relation(subject_id, predicate, sort_order);
            CREATE INDEX idx_rule_check ON rule_version(check_id, status, priority);
            """
        )
        rule_set = payload["rule_set"]
        connection.execute(
            "INSERT INTO snapshot_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                LOCAL_DATABASE_SCHEMA_VERSION,
                payload["snapshot_id"],
                payload["ontology_version"],
                rule_set["rule_set_code"],
                rule_set["version"],
                rule_set["process"],
                rule_set["scope_type"],
                rule_set["scope_key"],
                digest,
                payload["published_at"],
                _utc_now(),
            ),
        )
        connection.executemany(
            "INSERT INTO ontology_concept VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item["concept_id"],
                    item["concept_type"],
                    item["name_zh"],
                    item.get("name_en"),
                    item["definition"],
                    _json(item.get("aliases", [])),
                    _json(item["data_schema"])
                    if item.get("data_schema") is not None
                    else None,
                    _json(item.get("properties", {})),
                    item.get("status", "active"),
                )
                for item in payload["concepts"]
            ],
        )
        connection.executemany(
            "INSERT INTO ontology_relation VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    item["relation_id"],
                    item["subject_id"],
                    item["predicate"],
                    item["object_id"],
                    _json(item.get("qualifiers", {})),
                    int(item.get("sort_order", 0)),
                )
                for item in payload["relations"]
            ],
        )
        connection.executemany(
            "INSERT INTO factor_option VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    item["factor_id"],
                    item["option_code"],
                    item["name_zh"],
                    _json(item.get("value")),
                    int(item.get("sort_order", 0)),
                    item.get("status", "active"),
                )
                for item in payload["factor_options"]
            ],
        )
        connection.executemany(
            "INSERT INTO rule_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item["rule_version_id"],
                    item["rule_id"],
                    str(item["version"]),
                    item["check_id"],
                    item.get("name", item["rule_id"]),
                    _json(item.get("conditions", [])),
                    _json(item["expression"]),
                    item["comparator"],
                    _json(item.get("threshold")),
                    item.get("result_unit"),
                    item.get("severity", "warning"),
                    item.get("recommendation_template"),
                    item.get("explanation_text"),
                    int(item.get("priority", 0)),
                    1 if item.get("is_default") else 0,
                    item["status"],
                    _json(item.get("citation_refs", [])),
                    _content_hash(item),
                )
                for item in payload["rules"]
            ],
        )
        connection.commit()
        connection.execute("PRAGMA foreign_key_check")

    @staticmethod
    def _concept_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "concept_id": row["concept_id"],
            "concept_type": row["concept_type"],
            "name_zh": row["name_zh"],
            "name_en": row["name_en"],
            "definition": row["definition"],
            "aliases": _load_json(row["aliases_json"], []),
            "data_schema": _load_json(row["data_schema_json"], None),
            "properties": _load_json(row["properties_json"], {}),
            "status": row["status"],
        }

    @staticmethod
    def _relation_context_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "relation_id": row["relation_id"],
            "predicate": row["predicate"],
            "object": {
                "concept_id": row["object_id"],
                "concept_type": row["object_type"],
                "name_zh": row["object_name_zh"],
                "definition": row["object_definition"],
                "data_schema": _load_json(row["object_data_schema_json"], None),
                "properties": _load_json(row["object_properties_json"], {}),
            },
            "qualifiers": _load_json(row["qualifiers_json"], {}),
        }

    @staticmethod
    def _factor_option_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "factor_id": row["factor_id"],
            "option_code": row["option_code"],
            "name_zh": row["name_zh"],
            "value": _load_json(row["value_json"], None),
        }

    @staticmethod
    def _rule_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_version_id": row["rule_version_id"],
            "rule_id": row["rule_id"],
            "version": row["version"],
            "check_id": row["check_id"],
            "name": row["name"],
            "conditions": _load_json(row["conditions_json"], []),
            "expression": _load_json(row["expression_json"], None),
            "comparator": row["comparator"],
            "threshold": _load_json(row["threshold_json"], None),
            "result_unit": row["result_unit"],
            "severity": row["severity"],
            "recommendation_template": row["recommendation_template"],
            "explanation_text": row["explanation_text"],
            "priority": row["priority"],
            "is_default": bool(row["is_default"]),
            "citation_refs": _load_json(row["citation_refs_json"], []),
            "content_sha256": row["content_sha256"],
        }

    @staticmethod
    def _normalize_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, raw in facts.items():
            value = raw.get("value") if isinstance(raw, Mapping) else raw
            normalized[str(key)] = value
        return normalized

    @classmethod
    def _select_rule(
        cls,
        rows: Sequence[sqlite3.Row],
        facts: Mapping[str, Any],
        check_id: str,
    ) -> sqlite3.Row | None:
        matched = []
        missing_factors: set[str] = set()
        for row in rows:
            conditions = _load_json(row["conditions_json"], [])
            missing_factors.update(
                str(item["factor_id"])
                for item in conditions
                if str(item["factor_id"]) not in facts
            )
            if all(cls._condition_matches(item, facts) for item in conditions):
                matched.append((row, len(conditions)))
        non_default = [item for item in matched if not bool(item[0]["is_default"])]
        candidates = non_default or matched
        if not candidates:
            if missing_factors:
                return None
            if rows:
                raise DFMError(
                    "ontology_rule_not_found",
                    "No released rule matches the confirmed factors for a DFM Check.",
                    {"check_id": check_id},
                )
            return None
        candidates.sort(
            key=lambda item: (int(item[0]["priority"]), item[1]), reverse=True
        )
        winner, specificity = candidates[0]
        conflicting = [
            row
            for row, item_specificity in candidates[1:]
            if int(row["priority"]) == int(winner["priority"])
            and item_specificity == specificity
            and (
                row["threshold_json"] != winner["threshold_json"]
                or row["expression_json"] != winner["expression_json"]
                or row["comparator"] != winner["comparator"]
            )
        ]
        if conflicting:
            raise DFMError(
                "ontology_rule_conflict",
                "Multiple equally specific released rules produce different evaluations.",
                {
                    "check_id": check_id,
                    "rule_ids": [
                        winner["rule_id"],
                        *[row["rule_id"] for row in conflicting],
                    ],
                },
            )
        return winner

    @staticmethod
    def _condition_matches(
        condition: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> bool:
        factor_id = str(condition["factor_id"])
        if factor_id not in facts:
            return False
        actual = facts[factor_id]
        operator = str(condition["operator"])
        expected = condition.get("value")
        if operator == "EXISTS":
            return actual is not None
        if operator == "EQ":
            return actual == expected
        if operator == "IN":
            return isinstance(expected, list) and actual in expected
        if operator == "BETWEEN":
            return (
                isinstance(actual, (int, float))
                and not isinstance(actual, bool)
                and isinstance(expected, Mapping)
                and cls._finite(expected.get("lower"))
                and cls._finite(expected.get("upper"))
                and float(expected["lower"])
                <= float(actual)
                <= float(expected["upper"])
            )
        if not cls._finite(actual) or not cls._finite(expected):
            return False
        left, right = float(actual), float(expected)
        return {
            "GT": left > right,
            "GTE": left >= right,
            "LT": left < right,
            "LTE": left <= right,
        }.get(operator, False)

    @staticmethod
    def _finite(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    @classmethod
    def _compile_binding(
        cls,
        connection: sqlite3.Connection,
        check_id: str,
        rule: sqlite3.Row,
        operand_rows: Sequence[sqlite3.Row],
        operations: Sequence[PlanOperation],
        factor_runtime_keys: Mapping[str, str],
    ) -> tuple[RuleBinding, dict[str, dict[str, str]]]:
        if not operand_rows:
            cls._invalid(
                "A released Check rule does not declare any Measurement operand.",
                {"check_id": check_id},
            )
        operands: list[RuleOperand] = []
        selectors: dict[str, dict[str, str]] = {}
        for row in operand_rows:
            qualifiers = _load_json(row["qualifiers_json"], {})
            target = cls._resolve_operand_target(connection, check_id, row)
            alias = target["alias"]
            metric_id = target["metric_id"]
            quantity_id = target["quantity_id"]
            operation = cls._operation_for_operand(
                operations, metric_id, quantity_id, check_id=check_id, alias=alias
            )
            operand = RuleOperand(
                alias=alias,
                operation_id=operation.operation_id,
                metric_id=metric_id,
                quantity_id=quantity_id,
                aggregation=str(qualifiers.get("aggregation") or "identity"),
            )
            operand.validate()
            operands.append(operand)
            selectors[alias] = {
                "feature_type_id": target["feature_type_id"],
                "region_type_id": target["region_type_id"],
                "feature_kind": target["feature_kind"],
                "region_role": target["region_role"],
            }
        primary, *additional = operands
        conditions = _load_json(rule["conditions_json"], [])
        binding = RuleBinding(
            binding_id=f"binding.{check_id}.{rule['rule_id']}",
            operation_id=primary.operation_id,
            metric_id=primary.metric_id,
            quantity_id=primary.quantity_id,
            rule_id=str(rule["rule_id"]),
            operator=_COMPARATORS[str(rule["comparator"])],
            aggregation=primary.aggregation,
            required_fact_names=sorted({
                factor_runtime_keys.get(str(item["factor_id"]), str(item["factor_id"]))
                for item in conditions
            }),
            check_id=check_id,
            operand_alias=primary.alias,
            additional_operands=additional,
            expression=_load_json(rule["expression_json"], None),
        )
        binding.validate()
        return binding, selectors

    @classmethod
    def _resolve_operand_target(
        cls,
        connection: sqlite3.Connection,
        check_id: str,
        operand_row: sqlite3.Row,
    ) -> dict[str, str]:
        """Resolve an Operand through Metric and Check/Region/Feature relations."""

        qualifiers = _load_json(operand_row["qualifiers_json"], {})
        metric_properties = _load_json(operand_row["metric_properties_json"], {})
        alias = str(qualifiers.get("alias") or "")
        metric_id = str(
            metric_properties.get("worker_metric_id")
            or qualifiers.get("worker_metric_id")
            or ""
        )
        quantity_id = str(
            metric_properties.get("quantity_id") or qualifiers.get("quantity_id") or ""
        )
        if not alias or not metric_id or not quantity_id:
            cls._invalid(
                "An executable ontology operand lacks its alias or Metric capability mapping.",
                {"check_id": check_id, "operand_alias": alias},
            )

        region_rows = connection.execute(
            """
            SELECT relation.relation_id, relation.object_id, relation.qualifiers_json,
                   region.properties_json AS region_properties_json
            FROM ontology_relation AS relation
            JOIN ontology_concept AS region ON region.concept_id = relation.object_id
            WHERE relation.subject_id = ?
              AND relation.predicate = 'APPLIES_TO_REGION'
              AND region.concept_type = 'region_type'
              AND region.status = 'active'
            ORDER BY relation.sort_order, relation.relation_id
            """,
            (check_id,),
        ).fetchall()
        if not region_rows:
            # Compatibility for already-installed schema-1 publications. New
            # publications are rejected if they duplicate these selectors.
            legacy = {
                "alias": alias,
                "metric_id": metric_id,
                "quantity_id": quantity_id,
                "feature_type_id": str(qualifiers.get("feature_type_id") or ""),
                "region_type_id": str(qualifiers.get("region_type_id") or ""),
                "feature_kind": str(qualifiers.get("feature_kind") or ""),
                "region_role": str(qualifiers.get("region_role") or ""),
            }
            if not legacy["feature_kind"] or not legacy["region_role"]:
                cls._invalid(
                    "An operand has neither an APPLIES_TO_REGION path nor legacy selectors.",
                    {"check_id": check_id, "operand_alias": alias},
                )
            return legacy

        relation_payloads = [
            {
                "relation_id": str(row["relation_id"]),
                "object_id": str(row["object_id"]),
                "qualifiers": _load_json(row["qualifiers_json"], {}),
            }
            for row in region_rows
        ]
        selected = cls._select_region_relation_payload(
            check_id, alias, relation_payloads
        )
        region_id = str(selected["object_id"])
        region_row = next(row for row in region_rows if row["object_id"] == region_id)
        region_properties = _load_json(region_row["region_properties_json"], {})
        feature_rows = connection.execute(
            """
            SELECT feature.concept_id, feature.properties_json
            FROM ontology_relation AS parent
            JOIN ontology_concept AS feature ON feature.concept_id = parent.subject_id
            JOIN ontology_relation AS applies
              ON applies.object_id = feature.concept_id
             AND applies.subject_id = ?
             AND applies.predicate = 'APPLIES_TO_FEATURE'
            WHERE parent.predicate = 'HAS_REGION'
              AND parent.object_id = ?
              AND feature.concept_type = 'feature_type'
              AND feature.status = 'active'
            ORDER BY feature.concept_id
            """,
            (check_id, region_id),
        ).fetchall()
        if len(feature_rows) != 1:
            cls._invalid(
                "An operand Region must resolve to exactly one applicable Feature.",
                {
                    "check_id": check_id,
                    "operand_alias": alias,
                    "region_type_id": region_id,
                    "matching_feature_type_ids": [
                        str(row["concept_id"]) for row in feature_rows
                    ],
                },
            )
        feature = feature_rows[0]
        feature_properties = _load_json(feature["properties_json"], {})
        feature_kind = str(feature_properties.get("worker_kind") or "")
        region_role = str(region_properties.get("worker_role") or "")
        if not feature_kind or not region_role:
            cls._invalid(
                "Resolved Feature and Region concepts lack worker selectors.",
                {"check_id": check_id, "operand_alias": alias},
            )
        return {
            "alias": alias,
            "metric_id": metric_id,
            "quantity_id": quantity_id,
            "feature_type_id": str(feature["concept_id"]),
            "region_type_id": region_id,
            "feature_kind": feature_kind,
            "region_role": region_role,
        }

    @classmethod
    def _operation_for_operand(
        cls,
        operations: Sequence[PlanOperation],
        metric_id: str,
        quantity_id: str,
        *,
        check_id: str,
        alias: str,
    ) -> PlanOperation:
        matches = [
            operation
            for operation in operations
            if metric_id in operation.metric_ids
            and quantity_id in operation.required_quantities
        ]
        if len(matches) != 1:
            raise DFMError(
                "ontology_capability_mismatch",
                "A Check operand must resolve to exactly one declared geometry operation.",
                {
                    "check_id": check_id,
                    "operand_alias": alias,
                    "metric_id": metric_id,
                    "quantity_id": quantity_id,
                    "matching_operation_ids": [item.operation_id for item in matches],
                },
            )
        return matches[0]

    @staticmethod
    def _invalid(message: str, details: dict[str, Any] | None = None) -> None:
        raise DFMError("ontology_snapshot_invalid", message, details)
