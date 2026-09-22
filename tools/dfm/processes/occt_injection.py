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
from ..contracts import PlanOperation, RuleOperand
from ..errors import DFMError
from .base import ProcessPlan
from .injection import InjectionProcessAdapter


OCCT_SCOPE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scopes"
    / "injection"
    / "geometry_core_v4.json"
)


def _load_operations() -> list[PlanOperation]:
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
) -> ProcessPlan:
    """Reuse remote rules/facts while replacing only the geometry calculators."""

    base = adapter.compile(context, raw_parameters)
    operations = _load_operations()
    base_by_calculator = {item.calculator_id: item for item in base.operations}

    load = base_by_calculator.get("load_geometry")
    draft = base_by_calculator.get("measure_draft")
    enriched: list[PlanOperation] = []
    for operation in operations:
        arguments = dict(operation.arguments)
        required_fact_names = list(operation.required_fact_names)
        if operation.calculator_id == "geometry_preflight" and load is not None:
            model_unit = load.arguments.get("model_unit")
            if model_unit is not None:
                arguments["model_unit"] = model_unit
            required_fact_names = ["model_units"]
        if operation.calculator_id in {"measure_draft", "measure_undercut"}:
            pull_direction = draft.arguments.get("pull_direction") if draft else None
            if pull_direction is not None:
                arguments["pull_direction"] = pull_direction
            required_fact_names = ["model_units", "pull_dir"]
        elif operation.calculator_id == "measure_wall_thickness":
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
