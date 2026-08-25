"""Stable process-adapter boundary for DFM plan compilation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ..analyzers.base import AnalyzerContext
from ..contracts import Capability, EffectiveRule, PlanOperation, RuleBinding


@dataclass(frozen=True)
class ProcessPlan:
    process: str
    adapter_version: str
    scope_id: str
    scope_version: str
    rules: dict[str, EffectiveRule]
    operations: list[PlanOperation]
    accepted_inputs: set[str]
    rule_bindings: list[RuleBinding] = field(default_factory=list)
    binding_selectors: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=dict
    )
    ontology_snapshot_id: str = ""
    ontology_snapshot_sha256: str = ""


@dataclass(frozen=True)
class FactRequirement:
    """One explicit engineering prerequisite and the phase it blocks."""

    name: str
    question: str
    phase: str
    required_by: tuple[str, ...] = ()


@runtime_checkable
class ProcessAdapter(Protocol):
    key: str
    version: str

    def capability(self, context: AnalyzerContext) -> Capability: ...

    def required_facts(self) -> Mapping[str, str]: ...

    def fact_requirements(self) -> tuple[FactRequirement, ...]: ...

    def compile(
        self,
        context: AnalyzerContext,
        raw_parameters: Mapping[str, Any],
    ) -> ProcessPlan: ...
