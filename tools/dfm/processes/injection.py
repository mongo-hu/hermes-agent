"""Backend-neutral injection plan for wall-thickness and draft checks."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from ..analyzers.base import AnalyzerContext
from ..contracts import (
    Capability,
    CapabilityStatus,
    PlanOperation,
    ResolvedArgument,
)
from ..errors import DFMError
from ..ontology import LocalOntologyStore
from .base import FactRequirement, ProcessPlan


_TRUSTED_SOURCES = {"project_fact", "user_confirmed"}


class InjectionProcessAdapter:
    key = "injection"
    version = "injection-ontology-runtime-v1"

    def __init__(
        self,
        scope_path: Path | None = None,
        ontology_store: LocalOntologyStore | None = None,
    ) -> None:
        self.scope_path = scope_path or (
            Path(__file__).resolve().parents[1]
            / "scopes"
            / "injection"
            / "geometry_capability_v1.json"
        )
        self.ontology_store = ontology_store or LocalOntologyStore.from_package(
            Path(__file__).resolve().parents[1]
            / "scopes"
            / "injection"
            / "ontology_snapshot_v2.json"
        )

    def capability(self, context: AnalyzerContext) -> Capability:
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "The backend-neutral injection wall/draft scope is available.",
            details={"adapter_version": self.version},
        )

    def required_facts(self) -> Mapping[str, str]:
        return {item.name: item.question for item in self.fact_requirements()}

    def fact_requirements(self) -> tuple[FactRequirement, ...]:
        ontology_requirements = tuple(
            FactRequirement(
                name=str(item["name"]),
                question=str(item["question"]),
                phase=str(item["phase"]),
                required_by=tuple(str(value) for value in item["required_by"]),
            )
            for item in self.ontology_store.fact_requirements(self.key)
        )
        return (
            FactRequirement(
                name="process",
                question="Should this project be analyzed as injection molding or die casting?",
                phase="discovery",
                required_by=("feature.process_semantics",),
            ),
            *ontology_requirements,
        )

    def compile(
        self,
        context: AnalyzerContext,
        raw_parameters: Mapping[str, Any],
    ) -> ProcessPlan:
        scope = self._load_scope()
        defaults = scope["parameters"]
        accepted_inputs = set(defaults) | {
            item.name for item in self.fact_requirements() if item.name != "process"
        }
        unknown = sorted(set(raw_parameters) - accepted_inputs)
        if unknown:
            self._invalid("Unknown injection parameter.", {"parameters": unknown})

        resolved = {
            key: {
                "value": self._normalize_value(key, definition["value"]),
                "unit": definition.get("unit"),
                "source": "injection_scope_default",
                "source_ref": (
                    f"capability:{scope['capability_id']}@{scope['version']}"
                    f"/parameters/{key}"
                ),
                "kind": str(definition.get("kind") or "rule"),
            }
            for key, definition in defaults.items()
        }
        ontology_facts: dict[str, Any] = {}
        for key, raw in raw_parameters.items():
            source = "project_fact"
            value = raw
            if isinstance(raw, Mapping):
                value = raw.get("value")
                source = str(raw.get("source") or "")
            if source not in _TRUSTED_SOURCES:
                self._invalid(
                    "Injection parameter source is not trusted.",
                    {"parameter": key, "source": source},
                )
            normalized = self._normalize_value(key, value)
            ontology_facts[key] = normalized
            if key in defaults:
                resolved[key] = {
                    "value": normalized,
                    "unit": defaults[key].get("unit"),
                    "source": source,
                    "source_ref": str(raw.get("source_ref") or f"fact:{key}")
                    if isinstance(raw, Mapping)
                    else f"fact:{key}",
                    "kind": "engineering_context",
                }

        operations = [PlanOperation.from_dict(item) for item in scope["operations"]]
        compiled = self.ontology_store.compile(self.key, ontology_facts, operations)
        enriched_operations = []
        for operation in operations:
            arguments = dict(operation.arguments)
            algorithm_options = dict(operation.algorithm_options)
            if operation.calculator_id == "load_geometry":
                units = resolved["model_units"]
                arguments["model_unit"] = ResolvedArgument(
                    units["value"], units["source_ref"], None
                )
            elif operation.calculator_id == "measure_draft":
                pull = resolved["pull_dir"]
                arguments["pull_direction"] = ResolvedArgument(
                    pull["value"], pull["source_ref"], pull["unit"]
                )
            enriched_operations.append(
                PlanOperation(
                    operation_id=operation.operation_id,
                    calculator_id=operation.calculator_id,
                    depends_on=operation.depends_on,
                    metric_ids=operation.metric_ids,
                    required_quantities=operation.required_quantities,
                    required_artifacts=operation.required_artifacts,
                    required_fact_names=operation.required_fact_names,
                    feature_refs=operation.feature_refs,
                    region_refs=operation.region_refs,
                    arguments=arguments,
                    algorithm_options=algorithm_options,
                )
            )

        return ProcessPlan(
            process=self.key,
            adapter_version=self.version,
            scope_id=compiled.identity.scope_id,
            scope_version=compiled.identity.scope_version,
            rules=compiled.rules,
            operations=enriched_operations,
            accepted_inputs=accepted_inputs,
            rule_bindings=compiled.rule_bindings,
            binding_selectors=compiled.binding_selectors,
            ontology_snapshot_id=compiled.identity.snapshot_id,
            ontology_snapshot_sha256=compiled.identity.content_sha256,
        )

    def _load_scope(self) -> dict[str, Any]:
        try:
            scope = json.loads(self.scope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DFMError(
                "process_scope_invalid",
                "The injection default analysis scope could not be loaded.",
                {"path": str(self.scope_path)},
            ) from exc
        if (
            scope.get("capability_id") != "injection.geometry.wall-draft"
            or scope.get("version") != "1.0.0"
            or scope.get("process") != self.key
            or not isinstance(scope.get("parameters"), dict)
            or not isinstance(scope.get("operations"), list)
        ):
            raise DFMError(
                "process_scope_invalid",
                "The injection default analysis scope has an invalid contract.",
            )
        return scope

    def _normalize_value(self, key: str, value: Any) -> Any:
        if key == "material":
            material = str(value or "").strip().upper()
            if not material:
                self._invalid("material must be a non-empty material identifier.")
            return material
        if key == "model_units":
            unit = str(value or "").strip().lower()
            if unit not in {"mm", "millimeter", "millimeters"}:
                self._invalid(
                    "The frozen injection scope currently requires millimeter geometry.",
                    {"model_units": value, "supported_units": ["mm"]},
                )
            return "mm"
        if key == "pull_dir":
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                self._invalid("pull_dir must contain exactly three numbers.")
            try:
                vector = [float(item) for item in value]
            except (TypeError, ValueError) as exc:
                raise DFMError(
                    "process_parameter_invalid",
                    "pull_dir must contain exactly three numbers.",
                ) from exc
            if not all(math.isfinite(item) for item in vector) or not any(vector):
                self._invalid("pull_dir must be a finite non-zero vector.")
            return vector
        return value

    @staticmethod
    def _invalid(message: str, details: dict[str, Any] | None = None) -> None:
        raise DFMError("process_parameter_invalid", message, details)
