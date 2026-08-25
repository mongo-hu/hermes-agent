"""Die-casting process adapter with an isolated, versioned first scope."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..analyzers.base import AnalyzerContext
from ..contracts import Capability, CapabilityStatus, PlanOperation, ResolvedArgument
from ..errors import DFMError
from .base import FactRequirement, ProcessPlan


class DieCastingProcessAdapter:
    key = "die_casting"
    version = "die-casting-v1"

    def __init__(self, scope_path: Path | None = None) -> None:
        self.scope_path = scope_path or (
            Path(__file__).resolve().parents[1]
            / "scopes"
            / "die_casting"
            / "baseline_v1.json"
        )

    def capability(self, context: AnalyzerContext) -> Capability:
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "The die-casting process adapter is available for the approved topology gate.",
            details={
                "adapter_version": self.version,
                "available_calculators": ["load_geometry", "inspect_topology"],
                "pending_rule_approval": [
                    "measure_wall_thickness",
                    "measure_draft",
                    "inspect_undercut",
                ],
            },
        )

    def required_facts(self) -> Mapping[str, str]:
        return {item.name: item.question for item in self.fact_requirements()}

    def fact_requirements(self) -> tuple[FactRequirement, ...]:
        return (
            FactRequirement(
                name="model_units",
                question="What length unit was used to author the die-cast part model?",
                phase="discovery",
                required_by=("geometry.load",),
            ),
        )

    def compile(
        self,
        context: AnalyzerContext,
        raw_parameters: Mapping[str, Any],
    ) -> ProcessPlan:
        if set(raw_parameters) - {"model_units"}:
            raise DFMError(
                "process_parameter_invalid",
                "The initial die-casting topology scope does not accept rule overrides.",
                {"parameters": sorted(raw_parameters)},
            )
        scope = self._load_scope()
        raw_unit = raw_parameters.get("model_units", "mm")
        if isinstance(raw_unit, Mapping):
            unit = str(raw_unit.get("value") or "").strip().lower()
            source_ref = str(raw_unit.get("source_ref") or "fact:model_units")
        else:
            unit = str(raw_unit or "").strip().lower()
            source_ref = "scope:die_casting.topology-baseline@1.0.0/model_units"
        if unit not in {"mm", "millimeter", "millimeters"}:
            raise DFMError(
                "process_parameter_invalid",
                "The frozen die-casting scope requires millimeter geometry.",
            )
        operations = [PlanOperation.from_dict(item) for item in scope["operations"]]
        load = operations[0]
        operations[0] = PlanOperation(
            operation_id=load.operation_id,
            calculator_id=load.calculator_id,
            depends_on=load.depends_on,
            metric_ids=load.metric_ids,
            required_quantities=load.required_quantities,
            required_artifacts=load.required_artifacts,
            required_fact_names=load.required_fact_names,
            feature_refs=load.feature_refs,
            region_refs=load.region_refs,
            arguments={"model_unit": ResolvedArgument("mm", source_ref)},
            algorithm_options=load.algorithm_options,
        )
        return ProcessPlan(
            process=self.key,
            adapter_version=self.version,
            scope_id=str(scope["scope_id"]),
            scope_version=str(scope["version"]),
            rules={},
            operations=operations,
            accepted_inputs={"model_units"},
        )

    def _load_scope(self) -> dict[str, Any]:
        try:
            scope = json.loads(self.scope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DFMError(
                "process_scope_invalid",
                "The die-casting baseline scope could not be loaded.",
                {"path": str(self.scope_path)},
            ) from exc
        if (
            scope.get("scope_id") != "die_casting.topology-baseline"
            or scope.get("version") != "1.0.0"
            or scope.get("process") != self.key
            or not isinstance(scope.get("parameters"), dict)
            or not isinstance(scope.get("operations"), list)
        ):
            raise DFMError(
                "process_scope_invalid",
                "The die-casting baseline scope has an invalid contract.",
            )
        return scope
