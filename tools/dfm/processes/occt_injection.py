"""Adapter bridge from the remote injection plan to dfm-geometry.exe.

The shared injection adapter and Objective Schema 4 remain the source of
Hermes semantics.  This module only substitutes the calculator operation graph
required by the currently deployed OCCT CLI protocol.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping

from ..analyzers.base import AnalyzerContext
from ..analyzers.occt import discover_geometry_executable, probe_geometry_executable
from ..contracts import PlanOperation, ResolvedArgument, RuleOperand
from ..errors import DFMError
from .base import ProcessPlan
from .injection import InjectionProcessAdapter


OCCT_SCOPE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scopes"
    / "injection"
    / "geometry_core_v4.json"
)


def geometry_binding_index(scope: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Validate and project the Geometric-ID execution contract for Hermes."""

    if scope.get("binding_mode") != "geometric_id":
        raise DFMError(
            "process_scope_invalid",
            "The OCCT geometry scope must use Geometric-ID binding mode.",
        )
    if scope.get("legacy_binding_fields_active") is not False:
        raise DFMError(
            "process_scope_invalid",
            "The OCCT geometry scope must disable legacy ontology binding fields.",
        )
    operations = {
        str(item.get("operation_id")): item
        for item in scope.get("operations", [])
        if isinstance(item, Mapping) and item.get("operation_id")
    }
    result: dict[str, dict[str, str]] = {}
    for raw in scope.get("geometric_bindings", []):
        if not isinstance(raw, Mapping):
            raise DFMError(
                "process_scope_invalid",
                "An OCCT Geometric-ID binding is not an object.",
            )
        concept_id = str(raw.get("concept_id") or "")
        if not concept_id or concept_id in result:
            raise DFMError(
                "process_scope_invalid",
                "OCCT Geometric-ID bindings must have unique non-empty IDs.",
                {"concept_id": concept_id},
            )
        if raw.get("support_status") != "supported":
            continue
        execution = raw.get("execution")
        if not isinstance(execution, Mapping):
            raise DFMError(
                "process_scope_invalid",
                "A supported Geometric ID lacks an execution recipe.",
                {"concept_id": concept_id},
            )
        discovery_id = str(execution.get("discovery_operation_id") or "")
        measurement_id = str(execution.get("measurement_operation_id") or "")
        selector = execution.get("result_selector")
        quantity_id = str(
            selector.get("quantity_id")
            if isinstance(selector, Mapping)
            else ""
        )
        feature_kind = str(execution.get("feature_kind") or "")
        region_role = str(execution.get("region_role") or "")
        discovery = operations.get(discovery_id)
        measurement = operations.get(measurement_id)
        metric_ids = (
            measurement.get("metric_ids", [])
            if isinstance(measurement, Mapping)
            else []
        )
        required_quantities = (
            measurement.get("required_quantities", [])
            if isinstance(measurement, Mapping)
            else []
        )
        if (
            discovery is None
            or measurement is None
            or discovery.get("status") != "available"
            or measurement.get("status") != "available"
            or len(metric_ids) != 1
            or quantity_id not in required_quantities
            or not feature_kind
            or not region_role
        ):
            raise DFMError(
                "process_scope_invalid",
                "A supported Geometric-ID recipe does not match declared OCCT operations.",
                {"concept_id": concept_id},
            )
        result[concept_id] = {
            "metric_id": str(metric_ids[0]),
            "quantity_id": quantity_id,
            "feature_kind": feature_kind,
            "region_role": region_role,
            "discovery_operation_id": discovery_id,
            "measurement_operation_id": measurement_id,
        }
    return result


def probe_occt_geometry_capability(
    configured_executable: str = "",
) -> dict[str, Any] | None:
    """Read Geometric-ID bindings through the existing capabilities API."""

    resolved = discover_geometry_executable(configured_executable)
    if resolved is None:
        return None
    payload = probe_geometry_executable(resolved)
    if not isinstance(payload.get("geometric_bindings"), list):
        # An older executable can still serve publications that retain the
        # legacy fields. It simply cannot activate Geometric-ID-only rows.
        return None
    geometry_binding_index(payload)
    return payload


def _load_operations(
    capability: Mapping[str, Any] | None = None,
) -> list[PlanOperation]:
    if capability is not None:
        geometry_binding_index(capability)
        declared = {
            str(item.get("operation_id")): item
            for item in capability.get("operations", [])
            if isinstance(item, Mapping) and item.get("operation_id")
        }
        available = {
            operation_id
            for operation_id, item in declared.items()
            if item.get("status") == "available"
        }
        # A module is executable only when its complete dependency closure is
        # also available. Partially filled factories therefore stay out of plans.
        changed = True
        while changed:
            changed = False
            for operation_id in tuple(available):
                dependencies = declared[operation_id].get("depends_on", [])
                if any(str(dependency) not in available for dependency in dependencies):
                    available.remove(operation_id)
                    changed = True
        return [
            PlanOperation(
                operation_id=operation_id,
                calculator_id=str(declared[operation_id].get("calculator_id") or ""),
                depends_on=[
                    str(value)
                    for value in declared[operation_id].get("depends_on", [])
                ],
                metric_ids=[
                    str(value)
                    for value in declared[operation_id].get("metric_ids", [])
                ],
                required_quantities=[
                    str(value)
                    for value in declared[operation_id].get("required_quantities", [])
                ],
                required_artifacts=[
                    str(value)
                    for value in declared[operation_id].get("required_artifacts", [])
                ],
            )
            for operation_id in sorted(available)
        ]
    try:
        payload = json.loads(OCCT_SCOPE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DFMError(
            "process_scope_invalid",
            "The OCCT geometry operation scope could not be loaded.",
            {"path": str(OCCT_SCOPE_PATH)},
        ) from exc
    if (
        payload.get("scope_id") != "injection.geometry-core"
        or payload.get("version") != "4.0.0"
        or payload.get("process") != "injection"
        or not isinstance(payload.get("operations"), list)
    ):
        raise DFMError(
            "process_scope_invalid",
            "The OCCT geometry operation scope has an invalid identity.",
        )
    return [PlanOperation.from_dict(item) for item in payload["operations"]]


def preview_operations() -> list[PlanOperation]:
    """Return the minimum operation closure needed to render a STEP preview."""

    required = {"geometry.preflight", "topology.index", "topology.aag"}
    operations = [item for item in _load_operations() if item.operation_id in required]
    if {item.operation_id for item in operations} != required:
        raise DFMError(
            "preview_plan_invalid",
            "The OCCT scope lacks the operations required for a STEP preview.",
        )
    return operations


def compile_occt_injection_plan(
    adapter: InjectionProcessAdapter,
    context: AnalyzerContext,
    raw_parameters: Mapping[str, Any],
    capability: Mapping[str, Any] | None,
) -> ProcessPlan:
    """Reuse remote rules/facts while replacing only the geometry calculators."""

    operations = _load_operations(capability)
    base = adapter.compile(
        context,
        raw_parameters,
        operations_override=operations,
    )

    def resolved_argument(name: str, default: Any = None) -> ResolvedArgument:
        raw = raw_parameters.get(name, default)
        if isinstance(raw, Mapping):
            return ResolvedArgument(
                raw.get("value"),
                str(raw.get("source_ref") or f"fact:{name}"),
                raw.get("unit"),
            )
        return ResolvedArgument(raw, f"fact:{name}")

    enriched: list[PlanOperation] = []
    for operation in operations:
        arguments = dict(operation.arguments)
        required_fact_names = list(operation.required_fact_names)
        if operation.calculator_id == "geometry_preflight":
            arguments["model_unit"] = resolved_argument("model_units", "mm")
            required_fact_names = ["model_units"]
        if operation.calculator_id in {
            "measure_draft",
            "measure_undercut",
            "measure_main_wall_draft",
            "measure_screw_boss_draft",
        }:
            arguments["pull_direction"] = resolved_argument("pull_dir")
            required_fact_names = ["model_units", "pull_dir"]
        elif operation.metric_ids:
            required_fact_names = ["model_units"]
        enriched.append(
            replace(
                operation,
                arguments=arguments,
                required_fact_names=required_fact_names,
            )
        )

    def operation_for(metric_id: str, quantity_id: str) -> str | None:
        matches = [
            item.operation_id
            for item in enriched
            if metric_id in item.metric_ids and quantity_id in item.required_quantities
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def map_operand(operand: RuleOperand) -> RuleOperand | None:
        op_id = operation_for(operand.metric_id, operand.quantity_id)
        if op_id is None:
            return None
        return replace(operand, operation_id=op_id)

    bindings = []
    skipped_bindings: list[str] = []
    for binding in base.rule_bindings:
        primary_op = operation_for(binding.metric_id, binding.quantity_id)
        if primary_op is None:
            skipped_bindings.append(binding.binding_id)
            continue
        mapped_additionals = []
        skip = False
        for item in binding.additional_operands:
            mapped = map_operand(item)
            if mapped is None:
                skip = True
                break
            mapped_additionals.append(mapped)
        if skip:
            skipped_bindings.append(binding.binding_id)
            continue
        bindings.append(
            replace(
                binding,
                operation_id=primary_op,
                additional_operands=mapped_additionals,
            )
        )

    # Filter rules/selectors to only include non-skipped bindings
    skipped_binding_ids = set(skipped_bindings)
    filtered_rules = {
        rid: rule for rid, rule in base.rules.items()
        if not any(
            b.binding_id == f"binding.{b.check_id}.{rid}"
            for b in base.rule_bindings
            if b.binding_id in skipped_binding_ids
        )
    }
    filtered_selectors = {
        bid: sel for bid, sel in base.binding_selectors.items()
        if bid not in skipped_binding_ids
    }

    return ProcessPlan(
        process=base.process,
        adapter_version=f"{base.adapter_version}+occt-cli-v1",
        scope_id=base.scope_id,
        scope_version=base.scope_version,
        rules=filtered_rules,
        operations=enriched,
        accepted_inputs=base.accepted_inputs,
        rule_bindings=bindings,
        binding_selectors=filtered_selectors,
        ontology_snapshot_id=base.ontology_snapshot_id,
        ontology_snapshot_sha256=base.ontology_snapshot_sha256,
    )
