"""Versioned, JSON-compatible contracts shared by DFM services and tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import math
from pathlib import PurePosixPath
import re
from typing import Any

from .errors import DFMError


MANIFEST_SCHEMA_VERSION = 1
WORKER_SCHEMA_VERSION = 1
DISCOVERY_SCHEMA_VERSION = 1
OBJECTIVE_SCHEMA_VERSION = 4
GEOMETRY_REQUEST_CONTRACT = "dfm.geometry.request/v1"
GEOMETRY_EVENT_CONTRACT = "dfm.geometry.event/v1"
GEOMETRY_RESULT_CONTRACT = "dfm.geometry.result/v1"

STAGE_QUEUED = "queued"
STAGE_STARTING = "starting"
STAGE_OBJECTIVE_LOAD = "objective_load"
STAGE_OBJECTIVE_COMPUTE = "objective_compute"
STAGE_OBJECTIVE_MATERIALIZE = "objective_materialize"
STAGE_OBJECTIVE_READY = "objective_ready"
STAGE_RULE_EVALUATION = "rule_evaluation"
STAGE_EVIDENCE_RENDER = "evidence_render"
STAGE_REPORT_MATERIALIZE = "report_materialize"
STAGE_REPORT_EDITING = "report_editing"
STAGE_REPORT_QUEUED = "report_queued"
STAGE_REPORT_RENDERING = "report_rendering"
STAGE_REPORT_LAYOUT = "report_layout"
STAGE_REPORT_PACKAGING = "report_packaging"
STAGE_COMPLETE = "complete"


def normalize_objective_stage(stage: str | None) -> str:
    """Map backend-specific progress labels onto the shared runtime vocabulary."""

    value = str(stage or "").strip().lower()
    if value in {"queued", "accepted", "nx_queued", "pending"}:
        return STAGE_OBJECTIVE_LOAD
    if value in {"load", "loading", "load_geometry", "objective_load"}:
        return STAGE_OBJECTIVE_LOAD
    if value in {
        "materialize",
        "write_objective_artifacts",
        "download_artifacts",
        "objective_materialize",
    }:
        return STAGE_OBJECTIVE_MATERIALIZE
    if value in {"complete", "completed", "succeeded", "objective_ready"}:
        return STAGE_OBJECTIVE_READY
    return STAGE_OBJECTIVE_COMPUTE


def normalize_objective_error(code: str | None) -> str:
    """Collapse backend-specific failures into the public objective error taxonomy."""

    value = str(code or "").strip().lower()
    if value in {
        "schema_invalid",
        "unsupported_calculator",
        "unsupported_argument",
        "unsupported_quantity",
        "unsupported_region_mode",
        "objective_task_invalid",
        "worker_request_invalid",
    }:
        return "objective_task_invalid"
    if value in {"input_hash_mismatch", "objective_input_invalid"}:
        return "objective_input_invalid"
    if value in {"cancelled", "run_cancelled"}:
        return "run_cancelled"
    if value in {
        "license_unavailable",
        "nx_backend_unavailable",
        "backend_unavailable",
    }:
        return "objective_backend_unavailable"
    if value in {"nx_artifact_invalid", "artifact_invalid"}:
        return "objective_artifact_invalid"
    if value in {"nx_result_invalid", "worker_result_invalid"}:
        return "objective_result_invalid"
    if value in {"nx_execution_failed", "calculation_failed", "nx_analysis_failed"}:
        return "objective_calculation_failed"
    return value or "objective_backend_failed"


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    DEPENDENCY_MISSING = "dependency_missing"
    NOT_IMPLEMENTED = "not_implemented"
    BLOCKED = "blocked"
    DISABLED = "disabled"
    UNHEALTHY = "unhealthy"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    REPORTING = "reporting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


_RUN_TRANSITIONS = {
    RunStatus.QUEUED: {
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BLOCKED,
    },
    RunStatus.RUNNING: {
        RunStatus.REPORTING,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BLOCKED,
    },
    RunStatus.REPORTING: {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BLOCKED,
    },
    RunStatus.FAILED: {RunStatus.REPORTING},
}


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Raise when a persisted run attempts an invalid state transition."""

    if target not in _RUN_TRANSITIONS.get(current, set()):
        raise DFMError(
            "invalid_run_transition",
            f"Cannot transition DFM run from {current.value} to {target.value}.",
            {"current": current.value, "target": target.value},
        )


@dataclass(frozen=True)
class Capability:
    analyzer_key: str
    status: CapabilityStatus
    reason: str
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzer_key": self.analyzer_key,
            "status": self.status.value,
            "reason": self.reason,
            "error_code": self.error_code,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    name: str
    value: Any
    source: str
    status: str
    unit: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    version: str = "1"
    check_id: str | None = None
    feature_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FactRecord":
        return cls(**payload)


@dataclass(frozen=True)
class ClarificationRecord:
    clarification_id: str
    question: str
    status: str
    answer: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClarificationRecord":
        return cls(**payload)


@dataclass(frozen=True)
class FeatureRecord:
    feature_id: str
    kind: str
    source_refs: list[str]
    confidence: float
    input_sha256: str = ""
    region_refs: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    recognizer: str = ""
    recognizer_version: str = ""
    status: str = "detected"

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        if not self.region_refs:
            values.pop("region_refs")
        return values

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FeatureRecord":
        values = dict(payload)
        values.setdefault("region_refs", [])
        return cls(**values)


@dataclass(frozen=True)
class ObservationRecord:
    """A traceable document/model observation that is not yet a confirmed fact."""

    observation_id: str
    input_id: str
    kind: str
    value: Any
    source_refs: list[str]
    confidence: float
    status: str = "candidate"
    unit: str | None = None
    region_refs: list[str] = field(default_factory=list)
    feature_refs: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ObservationRecord":
        return cls(**payload)


@dataclass(frozen=True)
class FusionLinkRecord:
    """A reviewable link from an observation to 3D features and regions."""

    fusion_link_id: str
    observation_refs: list[str]
    feature_refs: list[str]
    region_refs: list[str]
    confidence: float
    status: str
    method: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FusionLinkRecord":
        return cls(**payload)


@dataclass(frozen=True)
class DiscoverySnapshotRecord:
    """Immutable identity of the observations and geometry used to compile rules."""

    snapshot_id: str
    created_at: str
    input_hashes: dict[str, str]
    observation_refs: list[str]
    feature_refs: list[str]
    region_refs: list[str]
    fusion_link_refs: list[str]
    provider_versions: dict[str, str]
    content_sha256: str
    status: str = "frozen"
    process: str = ""
    confirmed_fact_refs: list[str] = field(default_factory=list)
    geometry_snapshot_ref: str = ""
    topology_snapshot_id: str = ""
    render_mesh_snapshot_id: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    worker_snapshot_id: str = ""
    coverage: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    forming_scopes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        if not self.region_refs:
            values.pop("region_refs")
        return values

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiscoverySnapshotRecord":
        values = dict(payload)
        values.setdefault("region_refs", [])
        return cls(**values)


@dataclass(frozen=True)
class PlanRecord:
    plan_id: str
    input_mode: str
    analyzer_keys: list[str]
    status: str
    created_at: str
    process: str = ""
    process_adapter_version: str = ""
    scope_id: str = ""
    scope_version: str = ""
    ontology_snapshot_id: str = ""
    ontology_snapshot_sha256: str = ""
    input_ids: list[str] = field(default_factory=list)
    input_hashes: dict[str, str] = field(default_factory=dict)
    rules: dict[str, "EffectiveRule"] = field(default_factory=dict)
    rule_bindings: list["RuleBinding | GeometricRuleBinding"] = field(
        default_factory=list
    )
    operations: list["PlanOperation"] = field(default_factory=list)
    parent_plan_id: str | None = None
    invalidated_by: str | None = None
    affected_operation_ids: list[str] = field(default_factory=list)
    phase: str = "analysis"
    discovery_snapshot_refs: list[str] = field(default_factory=list)
    regions: list["RegionRecord"] = field(default_factory=list)
    plan_fragments: list["PlanFragmentRecord"] = field(default_factory=list)
    coverage_gaps: list[dict[str, Any]] = field(default_factory=list)
    pending_inputs: list[dict[str, Any]] = field(default_factory=list)

    def active_plan_fragments(
        self, *, statuses: set[str] | None = None
    ) -> tuple["PlanFragmentRecord", ...]:
        """Return the unsuperseded immutable fragments selected by this plan."""

        superseded = {
            item.supersedes
            for item in self.plan_fragments
            if item.supersedes is not None
        }
        return tuple(
            item
            for item in self.plan_fragments
            if item.fragment_id not in superseded
            and item.status not in {"stale", "cancelled"}
            and (statuses is None or item.status in statuses)
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        values = {
            "plan_id": self.plan_id,
            "input_mode": self.input_mode,
            "analyzer_keys": list(self.analyzer_keys),
            "status": self.status,
            "created_at": self.created_at,
            "process": self.process,
            "process_adapter_version": self.process_adapter_version,
            "scope_id": self.scope_id,
            "scope_version": self.scope_version,
            "ontology_snapshot_id": self.ontology_snapshot_id,
            "ontology_snapshot_sha256": self.ontology_snapshot_sha256,
            "input_ids": list(self.input_ids),
            "input_hashes": dict(self.input_hashes),
            "rules": {key: value.to_dict() for key, value in self.rules.items()},
            "rule_bindings": [item.to_dict() for item in self.rule_bindings],
            "parent_plan_id": self.parent_plan_id,
            "invalidated_by": self.invalidated_by,
            "phase": self.phase,
            "discovery_snapshot_refs": list(self.discovery_snapshot_refs),
            "plan_fragments": [item.to_dict() for item in self.plan_fragments],
            "coverage_gaps": [dict(item) for item in self.coverage_gaps],
            "pending_inputs": [dict(item) for item in self.pending_inputs],
        }
        if not self.plan_fragments:
            values["operations"] = [
                operation.to_dict() for operation in self.operations
            ]
            values["affected_operation_ids"] = list(self.affected_operation_ids)
            values["regions"] = [item.to_dict() for item in self.regions]
        return values

    def validate(self) -> None:
        if self.phase not in {"discovery", "analysis"}:
            raise DFMError(
                "plan_phase_invalid",
                "DFM plans must be either discovery or analysis plans.",
                {"plan_id": self.plan_id, "phase": self.phase},
            )
        if not isinstance(self.pending_inputs, list) or any(
            not isinstance(item, dict) or not item.get("clarification_id")
            for item in self.pending_inputs
        ):
            raise DFMError("plan_input_invalid", "Pending plan inputs require clarification identities.")
        if bool(self.ontology_snapshot_id) != bool(self.ontology_snapshot_sha256) or (
            self.ontology_snapshot_sha256
            and not re.fullmatch(r"[0-9a-f]{64}", self.ontology_snapshot_sha256)
        ):
            raise DFMError(
                "plan_ontology_snapshot_invalid",
                "A DFM plan must pin both ontology snapshot identity and content hash.",
                {"plan_id": self.plan_id},
            )
        binding_ids = [item.binding_id for item in self.rule_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Plan rule binding IDs must be unique.",
            )
        if self.plan_fragments and (
            self.operations or self.regions or self.affected_operation_ids
        ):
            raise DFMError(
                "plan_fragment_invalid",
                "A Geometric v2 plan cannot retain legacy operations or Regions.",
                {"plan_id": self.plan_id},
            )
        seen_fragment_ids: set[str] = set()
        for fragment in self.plan_fragments:
            fragment.validate()
            if fragment.fragment_id in seen_fragment_ids:
                raise DFMError(
                    "plan_fragment_invalid",
                    "Plan fragment IDs must be unique within one plan.",
                    {"fragment_id": fragment.fragment_id},
                )
            if fragment.supersedes is not None and (
                fragment.supersedes not in seen_fragment_ids
                or fragment.supersedes == fragment.fragment_id
            ):
                raise DFMError(
                    "plan_fragment_invalid",
                    "A replacement fragment must supersede an earlier fragment.",
                    {
                        "fragment_id": fragment.fragment_id,
                        "supersedes": fragment.supersedes,
                    },
                )
            seen_fragment_ids.add(fragment.fragment_id)
        if (
            any(
                isinstance(binding, GeometricRuleBinding)
                for binding in self.rule_bindings
            )
            and not self.plan_fragments
        ):
            raise DFMError(
                "plan_fragment_invalid",
                "A public G binding must be frozen into a Geometric plan fragment.",
                {"plan_id": self.plan_id},
            )
        operations = {item.operation_id: item for item in self.operations}
        regions = {item.region_id: item for item in self.regions}
        if len(regions) != len(self.regions):
            raise DFMError("plan_region_invalid", "Plan region IDs must be unique.")
        for operation in self.operations:
            missing = sorted(set(operation.region_refs) - set(regions))
            if missing:
                raise DFMError(
                    "plan_region_invalid",
                    "A plan operation references an unresolved region.",
                    {"operation_id": operation.operation_id, "region_refs": missing},
                )
        for binding in self.rule_bindings:
            binding.validate()
            if self.plan_fragments and not isinstance(binding, GeometricRuleBinding):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "A Geometric v2 plan accepts only Feature/G/context operands.",
                    {"binding_id": binding.binding_id},
                )
            if binding.rule_id not in self.rules:
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "A rule binding does not resolve within its plan.",
                    {"binding_id": binding.binding_id},
                )
            for operand in binding.measurement_operands():
                if isinstance(operand, GeometricRuleOperand):
                    if not any(
                        operand.geometric_id in item.geometric_ids
                        and (
                            not operand.feature_refs
                            or set(operand.feature_refs) == {item.feature_id}
                        )
                        and operand.context == item.context
                        for fragment in self.plan_fragments
                        for item in fragment.items
                    ):
                        raise DFMError(
                            "plan_rule_binding_invalid",
                            "A Geometric operand does not resolve within a plan fragment.",
                            {
                                "binding_id": binding.binding_id,
                                "operand_alias": operand.alias,
                                "geometric_id": operand.geometric_id,
                            },
                        )
                    continue
                operation = operations.get(operand.operation_id)
                if (
                    operation is None
                    or operand.metric_id not in operation.metric_ids
                    or operand.quantity_id not in operation.required_quantities
                ):
                    raise DFMError(
                        "plan_rule_binding_invalid",
                        "A rule operand does not resolve within its plan.",
                        {
                            "binding_id": binding.binding_id,
                            "operand_alias": operand.alias,
                        },
                    )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanRecord":
        values = dict(payload)
        values["rules"] = {
            key: EffectiveRule.from_dict(value)
            for key, value in values.get("rules", {}).items()
        }
        values["rule_bindings"] = [
            (
                GeometricRuleBinding.from_dict(value)
                if "geometric_id" in value
                else RuleBinding.from_dict(value)
            )
            for value in values.get("rule_bindings", [])
        ]
        values["operations"] = [
            PlanOperation.from_dict(value) for value in values.get("operations", [])
        ]
        values["regions"] = [
            RegionRecord.from_dict(value) for value in values.get("regions", [])
        ]
        values["plan_fragments"] = [
            PlanFragmentRecord.from_dict(value)
            for value in values.get("plan_fragments", [])
        ]
        plan = cls(**values)
        plan.validate()
        return plan


@dataclass(frozen=True)
class EffectiveRule:
    value: Any
    unit: str | None
    source: str
    version: str = "1"
    severity: str = "unclassified"
    severity_rationale: str | None = None
    name: str | None = None
    recommendation_template: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EffectiveRule":
        return cls(**payload)


_RULE_AGGREGATIONS = {
    "minimum",
    "maximum",
    "mean",
    "median",
    "sum",
    "count",
    "identity",
}
_RULE_OPERATORS = {">=", "<=", ">", "<", "==", "!=", "between"}
_EXPRESSION_ARITY = {
    "add": (2, None),
    "subtract": (2, 2),
    "multiply": (2, None),
    "divide": (2, 2),
    "minimum": (2, None),
    "maximum": (2, None),
    "abs": (1, 1),
    "negate": (1, 1),
}


def _expression_operand_aliases(
    expression: Any, *, binding_id: str, depth: int = 0
) -> set[str]:
    if depth > 32 or not isinstance(expression, dict):
        raise DFMError(
            "plan_rule_binding_invalid",
            "Rule expressions must be bounded JSON expression objects.",
            {"binding_id": binding_id},
        )
    if "operand" in expression:
        alias = expression.get("operand")
        if set(expression) != {"operand"} or not isinstance(alias, str) or not alias:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule expression operand nodes are invalid.",
                {"binding_id": binding_id},
            )
        return {alias}
    if "constant" in expression:
        value = expression.get("constant")
        unit = expression.get("unit")
        if (
            not set(expression).issubset({"constant", "unit"})
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or (unit is not None and not isinstance(unit, str))
        ):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule expression constant nodes are invalid.",
                {"binding_id": binding_id},
            )
        return set()
    operation = expression.get("op")
    arguments = expression.get("args")
    if (
        set(expression) != {"op", "args"}
        or operation not in _EXPRESSION_ARITY
        or not isinstance(arguments, list)
    ):
        raise DFMError(
            "plan_rule_binding_invalid",
            "Rule expression operation nodes are invalid.",
            {"binding_id": binding_id, "operation": operation},
        )
    minimum, maximum = _EXPRESSION_ARITY[operation]
    if len(arguments) < minimum or (maximum is not None and len(arguments) > maximum):
        raise DFMError(
            "plan_rule_binding_invalid",
            "Rule expression operation arity is invalid.",
            {"binding_id": binding_id, "operation": operation},
        )
    aliases: set[str] = set()
    for argument in arguments:
        aliases.update(
            _expression_operand_aliases(
                argument, binding_id=binding_id, depth=depth + 1
            )
        )
    return aliases


def _acceptance_criteria_groups(
    criteria: Any, *, binding_id: str
) -> list[list[dict[str, Any]]]:
    """Read legacy flat criteria or the Schema 4 OR-of-AND representation."""
    if not isinstance(criteria, list) or not criteria:
        raise DFMError(
            "plan_rule_binding_invalid",
            "Acceptance criteria must be a nonempty array.",
            {"binding_id": binding_id},
        )
    if all(isinstance(item, dict) for item in criteria):
        return [criteria]
    if all(
        isinstance(group, list)
        and group
        and all(isinstance(item, dict) for item in group)
        for group in criteria
    ):
        return criteria
    raise DFMError(
        "plan_rule_binding_invalid",
        "Acceptance criteria must be either one flat AND group or nonempty OR groups of nonempty AND criteria.",
        {"binding_id": binding_id},
    )


def _validate_acceptance_criteria(criteria: Any, *, binding_id: str) -> set[str]:
    """Validate Schema 3 flat AND or Schema 4 OR-of-AND acceptance criteria."""
    groups = _acceptance_criteria_groups(criteria, binding_id=binding_id)
    identifiers: set[str] = set()
    aliases: set[str] = set()
    for item in (criterion for group in groups for criterion in group):
        if not isinstance(item, dict) or set(item) != {
            "criterion_id",
            "expression",
            "comparator",
            "threshold",
            "result_unit",
        }:
            raise DFMError(
                "plan_rule_binding_invalid",
                "An acceptance criterion has invalid fields.",
                {"binding_id": binding_id},
            )
        criterion_id = item["criterion_id"]
        if (
            not isinstance(criterion_id, str)
            or not criterion_id.strip()
            or criterion_id in identifiers
        ):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Acceptance criterion IDs must be unique and nonempty.",
                {"binding_id": binding_id},
            )
        identifiers.add(criterion_id)
        item_aliases = _expression_operand_aliases(
            item["expression"], binding_id=binding_id
        )
        if not item_aliases:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Each acceptance criterion must reference an Operand.",
                {"binding_id": binding_id, "criterion_id": criterion_id},
            )
        aliases.update(item_aliases)
        comparator = item["comparator"]
        threshold = item["threshold"]
        if not isinstance(comparator, str) or comparator not in {
            "GT",
            "GTE",
            "LT",
            "LTE",
            "EQ",
            "NE",
            "BETWEEN",
        }:
            raise DFMError(
                "plan_rule_binding_invalid",
                "An acceptance comparator is unsupported.",
                {"binding_id": binding_id, "criterion_id": criterion_id},
            )
        finite = lambda value: type(value) in {int, float} and math.isfinite(value)
        if comparator == "BETWEEN":
            valid_threshold = (
                isinstance(threshold, dict)
                and set(threshold) == {"lower", "upper"}
                and finite(threshold["lower"])
                and finite(threshold["upper"])
                and threshold["lower"] <= threshold["upper"]
            )
        else:
            valid_threshold = finite(threshold)
        if not valid_threshold or not isinstance(item["result_unit"], str):
            raise DFMError(
                "plan_rule_binding_invalid",
                "An acceptance threshold or unit is invalid.",
                {"binding_id": binding_id, "criterion_id": criterion_id},
            )
    if not aliases:
        raise DFMError(
            "plan_rule_binding_invalid",
            "Acceptance criteria must reference at least one Operand.",
            {"binding_id": binding_id},
        )
    return aliases


@dataclass(frozen=True)
class RuleOperand:
    """Legacy non-v2 Measurement input retained for explicit old backends."""

    alias: str
    operation_id: str
    metric_id: str
    quantity_id: str
    aggregation: str = "identity"
    feature_refs: list[str] = field(default_factory=list)
    region_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuleOperand":
        operand = cls(**payload)
        operand.validate()
        return operand

    def validate(self) -> None:
        identities = (self.alias, self.operation_id, self.metric_id, self.quantity_id)
        if any(not isinstance(value, str) or not value for value in identities):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Legacy rule operands require complete operation, metric, and quantity identities.",
            )
        if self.aggregation not in _RULE_AGGREGATIONS:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule operand aggregation is unsupported.",
                {"operand_alias": self.alias, "aggregation": self.aggregation},
            )
        for name, refs in (
            ("feature_refs", self.feature_refs),
            ("region_refs", self.region_refs),
        ):
            if len(refs) != len(set(refs)) or any(
                not isinstance(ref, str) or not ref for ref in refs
            ):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    f"Rule operand {name} must contain unique stable identities.",
                    {"operand_alias": self.alias},
                )


@dataclass(frozen=True)
class RuleBinding:
    """Legacy non-v2 rule binding retained for explicit old backends."""

    binding_id: str
    operation_id: str
    metric_id: str
    quantity_id: str
    rule_id: str
    operator: str
    aggregation: str
    required_fact_names: list[str] = field(default_factory=list)
    feature_refs: list[str] = field(default_factory=list)
    region_refs: list[str] = field(default_factory=list)
    check_id: str = ""
    operand_alias: str = "actual"
    additional_operands: list[RuleOperand] = field(default_factory=list)
    expression: dict[str, Any] | None = None
    rule_selection: dict[str, Any] | None = None
    acceptance_criteria_json: list[dict[str, Any]] | list[list[dict[str, Any]]] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        values = {
            **asdict(self),
            "additional_operands": [
                operand.to_dict() for operand in self.additional_operands
            ],
        }
        if self.rule_selection is None:
            values.pop("rule_selection")
        return values

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuleBinding":
        values = dict(payload)
        values["additional_operands"] = [
            RuleOperand.from_dict(item)
            for item in values.get("additional_operands", [])
        ]
        binding = cls(**values)
        binding.validate()
        return binding

    def measurement_operands(self) -> tuple[RuleOperand, ...]:
        return (
            RuleOperand(
                alias=self.operand_alias,
                operation_id=self.operation_id,
                metric_id=self.metric_id,
                quantity_id=self.quantity_id,
                aggregation=self.aggregation,
                feature_refs=self.feature_refs,
                region_refs=self.region_refs,
            ),
            *self.additional_operands,
        )

    def validate(self) -> None:
        identities = (
            self.binding_id,
            self.operation_id,
            self.metric_id,
            self.quantity_id,
            self.rule_id,
            self.operand_alias,
        )
        if any(not isinstance(value, str) or not value for value in identities):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Legacy rule bindings require complete stable routing identities.",
            )
        if not isinstance(self.check_id, str):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding check_id must be a stable string identity.",
                {"binding_id": self.binding_id},
            )
        if self.operator not in _RULE_OPERATORS:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding operator is unsupported.",
                {"binding_id": self.binding_id, "operator": self.operator},
            )
        if self.aggregation not in _RULE_AGGREGATIONS:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding aggregation is unsupported.",
                {"binding_id": self.binding_id, "aggregation": self.aggregation},
            )
        if len(self.required_fact_names) != len(set(self.required_fact_names)) or any(
            not isinstance(name, str) or not name for name in self.required_fact_names
        ):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding required_fact_names must contain unique names.",
                {"binding_id": self.binding_id},
            )
        for name, refs in (
            ("feature_refs", self.feature_refs),
            ("region_refs", self.region_refs),
        ):
            if len(refs) != len(set(refs)) or any(
                not isinstance(ref, str) or not ref for ref in refs
            ):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    f"Rule binding {name} must contain unique stable identities.",
                    {"binding_id": self.binding_id},
                )
        operands = self.measurement_operands()
        for operand in operands:
            operand.validate()
        aliases = [operand.alias for operand in operands]
        if len(aliases) != len(set(aliases)):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule operand aliases must be unique within one binding.",
                {"binding_id": self.binding_id},
            )
        condition_aliases: set[str] = set()
        if self.rule_selection is not None:
            selection = self.rule_selection
            valid = (
                isinstance(selection, dict)
                and set(selection)
                == {"conditions", "priority", "specificity", "is_default"}
                and isinstance(selection.get("conditions"), list)
                and type(selection.get("priority")) is int
                and type(selection.get("specificity")) is int
                and selection["specificity"] >= len(selection["conditions"])
                and isinstance(selection.get("is_default"), bool)
                and bool(self.check_id)
            )
            if not valid:
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "Deferred rule selection metadata is invalid.",
                )
            for condition in selection["conditions"]:
                if (
                    not isinstance(condition, dict)
                    or set(condition) != {"geometric_id", "operator", "value", "unit"}
                    or not isinstance(condition.get("geometric_id"), str)
                    or condition["geometric_id"] not in aliases
                    or condition.get("operator") not in ("GT", "GTE", "LT", "LTE")
                    or not isinstance(condition.get("unit"), str)
                    or not isinstance(condition.get("value"), (int, float))
                    or isinstance(condition["value"], bool)
                    or not math.isfinite(condition["value"])
                ):
                    raise DFMError(
                        "plan_rule_binding_invalid",
                        "Deferred geometric condition is invalid.",
                    )
                condition_aliases.add(condition["geometric_id"])
        if self.additional_operands and (not self.check_id or self.expression is None):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Multi-Measurement bindings require check_id and an explicit expression.",
                {"binding_id": self.binding_id},
            )
        if self.acceptance_criteria_json:
            groups = _acceptance_criteria_groups(
                self.acceptance_criteria_json, binding_id=self.binding_id
            )
            first = groups[0][0]
            comparators = {
                "GT": ">",
                "GTE": ">=",
                "LT": "<",
                "LTE": "<=",
                "EQ": "==",
                "NE": "!=",
                "BETWEEN": "between",
            }
            if (
                not isinstance(first, dict)
                or not self.check_id
                or self.expression != first.get("expression")
                or self.operator != comparators.get(first.get("comparator"))
            ):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "The primary binding must match the first acceptance criterion.",
                    {"binding_id": self.binding_id},
                )
        if self.expression is not None:
            referenced = (
                _validate_acceptance_criteria(
                    self.acceptance_criteria_json, binding_id=self.binding_id
                )
                if self.acceptance_criteria_json
                else _expression_operand_aliases(
                    self.expression, binding_id=self.binding_id
                )
            )
            if referenced | condition_aliases != set(aliases):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "Rule expressions must reference every declared operand exactly by alias.",
                    {
                        "binding_id": self.binding_id,
                        "declared_aliases": sorted(aliases),
                        "referenced_aliases": sorted(referenced),
                    },
                )


@dataclass(frozen=True)
class GeometricRuleOperand:
    """One rule variable bound only by Feature + published G + context."""

    alias: str
    geometric_id: str
    aggregation: str = "identity"
    feature_refs: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricRuleOperand":
        operand = cls(**payload)
        operand.validate()
        return operand

    def validate(self) -> None:
        if (
            not isinstance(self.alias, str)
            or not self.alias
            or re.fullmatch(r"G_[A-Z0-9_]+", self.geometric_id) is None
        ):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Geometric operands require an alias and a published G identity.",
                {"operand_alias": self.alias, "geometric_id": self.geometric_id},
            )
        if self.aggregation not in _RULE_AGGREGATIONS:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule operand aggregation is unsupported.",
                {"operand_alias": self.alias, "aggregation": self.aggregation},
            )
        if len(self.feature_refs) != len(set(self.feature_refs)) or any(
            not isinstance(ref, str) or not ref for ref in self.feature_refs
        ):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Geometric operand feature_refs must contain unique stable identities.",
                {"operand_alias": self.alias},
            )
        if not isinstance(self.context, dict):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Geometric operand context must be an object.",
                {"operand_alias": self.alias},
            )


@dataclass(frozen=True)
class GeometricRuleBinding:
    """Bind a rule directly to published G operands without Worker route IDs."""

    binding_id: str
    geometric_id: str
    rule_id: str
    operator: str
    aggregation: str
    required_fact_names: list[str] = field(default_factory=list)
    feature_refs: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    check_id: str = ""
    operand_alias: str = "actual"
    additional_operands: list[GeometricRuleOperand] = field(default_factory=list)
    expression: dict[str, Any] | None = None
    rule_selection: dict[str, Any] | None = None
    acceptance_criteria_json: list[dict[str, Any]] | list[list[dict[str, Any]]] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        values = {
            **asdict(self),
            "additional_operands": [
                operand.to_dict() for operand in self.additional_operands
            ],
        }
        if self.rule_selection is None:
            values.pop("rule_selection")
        return values

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricRuleBinding":
        values = dict(payload)
        values["additional_operands"] = [
            GeometricRuleOperand.from_dict(item)
            for item in values.get("additional_operands", [])
        ]
        binding = cls(**values)
        binding.validate()
        return binding

    def measurement_operands(self) -> tuple[GeometricRuleOperand, ...]:
        return (
            GeometricRuleOperand(
                alias=self.operand_alias,
                geometric_id=self.geometric_id,
                aggregation=self.aggregation,
                feature_refs=self.feature_refs,
                context=self.context,
            ),
            *self.additional_operands,
        )

    def validate(self) -> None:
        identities = (self.binding_id, self.rule_id, self.operand_alias)
        if any(not isinstance(value, str) or not value for value in identities):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Geometric rule bindings require non-empty stable identities.",
            )
        primary = self.measurement_operands()[0]
        primary.validate()
        if not isinstance(self.check_id, str):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding check_id must be a stable string identity.",
                {"binding_id": self.binding_id},
            )
        if self.operator not in _RULE_OPERATORS:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding operator is unsupported.",
                {"binding_id": self.binding_id, "operator": self.operator},
            )
        if self.aggregation not in _RULE_AGGREGATIONS:
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding aggregation is unsupported.",
                {"binding_id": self.binding_id, "aggregation": self.aggregation},
            )
        if len(self.required_fact_names) != len(set(self.required_fact_names)) or any(
            not isinstance(name, str) or not name for name in self.required_fact_names
        ):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule binding required_fact_names must contain unique names.",
                {"binding_id": self.binding_id},
            )
        operands = self.measurement_operands()
        for operand in operands:
            operand.validate()
        aliases = [operand.alias for operand in operands]
        if len(aliases) != len(set(aliases)):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Rule operand aliases must be unique within one binding.",
                {"binding_id": self.binding_id},
            )
        condition_aliases: set[str] = set()
        if self.rule_selection is not None:
            selection = self.rule_selection
            valid = (
                isinstance(selection, dict)
                and set(selection)
                == {"conditions", "priority", "specificity", "is_default"}
                and isinstance(selection.get("conditions"), list)
                and type(selection.get("priority")) is int
                and type(selection.get("specificity")) is int
                and selection["specificity"] >= len(selection["conditions"])
                and isinstance(selection.get("is_default"), bool)
                and bool(self.check_id)
            )
            if not valid:
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "Deferred rule selection metadata is invalid.",
                )
            for condition in selection["conditions"]:
                if (
                    not isinstance(condition, dict)
                    or set(condition) != {"geometric_id", "operator", "value", "unit"}
                    or not isinstance(condition.get("geometric_id"), str)
                    or condition["geometric_id"] not in aliases
                    or condition.get("operator") not in ("GT", "GTE", "LT", "LTE")
                    or not isinstance(condition.get("unit"), str)
                    or not isinstance(condition.get("value"), (int, float))
                    or isinstance(condition["value"], bool)
                    or not math.isfinite(condition["value"])
                ):
                    raise DFMError(
                        "plan_rule_binding_invalid",
                        "Deferred geometric condition is invalid.",
                    )
                condition_aliases.add(condition["geometric_id"])
        if self.additional_operands and (not self.check_id or self.expression is None):
            raise DFMError(
                "plan_rule_binding_invalid",
                "Multi-Geometric bindings require check_id and an explicit expression.",
                {"binding_id": self.binding_id},
            )
        if self.acceptance_criteria_json:
            groups = _acceptance_criteria_groups(
                self.acceptance_criteria_json, binding_id=self.binding_id
            )
            first = groups[0][0]
            comparators = {
                "GT": ">",
                "GTE": ">=",
                "LT": "<",
                "LTE": "<=",
                "EQ": "==",
                "NE": "!=",
                "BETWEEN": "between",
            }
            if (
                not isinstance(first, dict)
                or not self.check_id
                or self.expression != first.get("expression")
                or self.operator != comparators.get(first.get("comparator"))
            ):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "The primary binding must match the first acceptance criterion.",
                    {"binding_id": self.binding_id},
                )
        if self.expression is not None:
            referenced = (
                _validate_acceptance_criteria(
                    self.acceptance_criteria_json, binding_id=self.binding_id
                )
                if self.acceptance_criteria_json
                else _expression_operand_aliases(
                    self.expression, binding_id=self.binding_id
                )
            )
            if referenced | condition_aliases != set(aliases):
                raise DFMError(
                    "plan_rule_binding_invalid",
                    "Rule expressions must reference every declared operand exactly by alias.",
                    {
                        "binding_id": self.binding_id,
                        "declared_aliases": sorted(aliases),
                        "referenced_aliases": sorted(referenced),
                    },
                )


@dataclass(frozen=True)
class GeometricRequest:
    """One public Worker v2 computation request inside an immutable plan fragment."""

    feature_id: str
    geometric_ids: list[str]
    context: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if (
            not isinstance(self.feature_id, str)
            or not self.feature_id
            or not self.geometric_ids
            or len(self.geometric_ids) != len(set(self.geometric_ids))
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"G_[A-Z0-9_]+", value) is None
                for value in self.geometric_ids
            )
            or not isinstance(self.context, dict)
        ):
            raise DFMError(
                "plan_fragment_invalid",
                "A plan fragment contains an invalid public Geometric request.",
                {"feature_id": self.feature_id},
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricRequest":
        request = cls(**payload)
        request.validate()
        return request


@dataclass(frozen=True)
class PlanFragmentRecord:
    """Immutable Hermes scheduling record; never sent to the geometry Worker."""

    fragment_id: str
    snapshot_id: str
    source_discovery_snapshot_id: str
    capability_version: str
    dependency_fingerprint: str
    confirmed_fact_refs: list[str]
    rule_binding_refs: list[str]
    items: list[GeometricRequest]
    status: str = "ready"
    supersedes: str | None = None
    review_group_refs: list[str] = field(default_factory=list)
    blocking_reasons: list[dict[str, Any]] = field(default_factory=list)
    reference_dependencies: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if (
            not self.fragment_id
            or not self.snapshot_id
            or not self.source_discovery_snapshot_id
            or not self.capability_version
            or re.fullmatch(r"[0-9a-f]{64}", self.dependency_fingerprint) is None
            or self.status
            not in {
                "waiting_input",
                "pending_review",
                "ready",
                "running",
                "completed",
                "needs_attention",
                "stale",
                "cancelled",
            }
            or not self.items
        ):
            raise DFMError(
                "plan_fragment_invalid",
                "A Geometric plan fragment has an invalid immutable identity.",
                {"fragment_id": self.fragment_id},
            )
        for refs, name in (
            (self.confirmed_fact_refs, "confirmed_fact_refs"),
            (self.rule_binding_refs, "rule_binding_refs"),
            (self.review_group_refs, "review_group_refs"),
        ):
            if len(refs) != len(set(refs)) or any(not value for value in refs):
                raise DFMError(
                    "plan_fragment_invalid",
                    f"Plan fragment {name} must contain unique stable identities.",
                    {"fragment_id": self.fragment_id},
                )
        for item in self.items:
            item.validate()
        for dependency in self.reference_dependencies:
            if (not isinstance(dependency, dict)
                or any(not isinstance(dependency.get(key), str) or not dependency[key] for key in (
                    "feature_id", "geometric_id", "relation_id", "type", "target_feature_id", "target_feature_kind"))
                or not isinstance(dependency.get("geometry_refs"), list) or not dependency["geometry_refs"]
                or not isinstance(dependency.get("review_group_refs"), list)):
                raise DFMError("plan_fragment_invalid", "A reference dependency is missing its geometric identity.")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = {
            **asdict(self),
            "items": [item.to_dict() for item in self.items],
        }
        if self.supersedes is None:
            payload.pop("supersedes")
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanFragmentRecord":
        values = dict(payload)
        values["items"] = [
            GeometricRequest.from_dict(item) for item in values.get("items", [])
        ]
        fragment = cls(**values)
        fragment.validate()
        return fragment


@dataclass(frozen=True)
class PlanOperation:
    operation_id: str
    calculator_id: str
    depends_on: list[str] = field(default_factory=list)
    metric_ids: list[str] = field(default_factory=list)
    required_quantities: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    arguments: dict[str, "ResolvedArgument"] = field(default_factory=dict)
    algorithm_options: dict[str, "ResolvedArgument"] = field(default_factory=dict)
    required_fact_names: list[str] = field(default_factory=list)
    feature_refs: list[str] = field(default_factory=list)
    region_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "operation_id": self.operation_id,
            "calculator_id": self.calculator_id,
            "depends_on": list(self.depends_on),
            "metric_ids": list(self.metric_ids),
            "required_quantities": list(self.required_quantities),
            "required_artifacts": list(self.required_artifacts),
            "required_fact_names": list(self.required_fact_names),
            "feature_refs": list(self.feature_refs),
            "region_refs": list(self.region_refs),
            "arguments": {
                key: value.to_dict() for key, value in self.arguments.items()
            },
            "algorithm_options": {
                key: value.to_dict() for key, value in self.algorithm_options.items()
            },
        }

    def validate(self) -> None:
        if not self.operation_id or not self.calculator_id:
            raise DFMError(
                "plan_operation_invalid",
                "Plan operations require operation_id and calculator_id.",
            )
        if len(self.metric_ids) != len(set(self.metric_ids)):
            raise DFMError(
                "plan_operation_invalid",
                "Plan operation metric_ids must be unique.",
                {"operation_id": self.operation_id},
            )
        if len(self.required_quantities) != len(set(self.required_quantities)):
            raise DFMError(
                "plan_operation_invalid",
                "Plan operation required_quantities must be unique.",
                {"operation_id": self.operation_id},
            )
        if len(self.required_artifacts) != len(set(self.required_artifacts)):
            raise DFMError(
                "plan_operation_invalid",
                "Plan operation required_artifacts must be unique.",
                {"operation_id": self.operation_id},
            )
        if len(self.required_fact_names) != len(set(self.required_fact_names)) or any(
            not isinstance(name, str) or not name for name in self.required_fact_names
        ):
            raise DFMError(
                "plan_operation_invalid",
                "Plan operation required_fact_names must contain unique names.",
                {"operation_id": self.operation_id},
            )
        for name, refs in (
            ("feature_refs", self.feature_refs),
            ("region_refs", self.region_refs),
        ):
            if len(refs) != len(set(refs)) or any(
                not isinstance(ref, str) or not ref for ref in refs
            ):
                raise DFMError(
                    "plan_operation_invalid",
                    f"Plan operation {name} must contain unique stable identities.",
                    {"operation_id": self.operation_id},
                )
        for values in (self.arguments, self.algorithm_options):
            for name, argument in values.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(argument, ResolvedArgument)
                ):
                    raise DFMError(
                        "plan_operation_invalid",
                        "Plan operation inputs must be named resolved arguments.",
                        {"operation_id": self.operation_id},
                    )
                argument.validate(name, self.operation_id)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PlanOperation":
        values = dict(payload)
        values["arguments"] = {
            key: ResolvedArgument.from_dict(value)
            for key, value in values.get("arguments", {}).items()
        }
        values["algorithm_options"] = {
            key: ResolvedArgument.from_dict(value)
            for key, value in values.get("algorithm_options", {}).items()
        }
        operation = cls(**values)
        operation.validate()
        return operation


@dataclass(frozen=True)
class ResolvedArgument:
    value: Any
    source_ref: str
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResolvedArgument":
        return cls(**payload)

    def validate(self, name: str, operation_id: str) -> None:
        if not self.source_ref:
            raise DFMError(
                "plan_operation_invalid",
                "Resolved operation arguments require source_ref provenance.",
                {"operation_id": operation_id, "argument": name},
            )


@dataclass(frozen=True)
class BoundingBox:
    minimum: list[float]
    maximum: list[float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BoundingBox":
        return cls(**payload)


@dataclass(frozen=True)
class RegionRecord:
    region_id: str
    input_sha256: str
    coordinate_system: str
    mode: str
    semantic_label: str
    source_refs: list[str]
    version: str
    content_sha256: str
    bbox: BoundingBox | None = None
    geometry_refs: list["GeometryRef"] = field(default_factory=list)
    excluded_geometry_refs: list["GeometryRef"] = field(default_factory=list)
    role: str = ""
    feature_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            **asdict(self),
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "geometry_refs": [item.to_dict() for item in self.geometry_refs],
            "excluded_geometry_refs": [
                item.to_dict() for item in self.excluded_geometry_refs
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegionRecord":
        values = dict(payload)
        if isinstance(values.get("bbox"), dict):
            values["bbox"] = BoundingBox.from_dict(values["bbox"])
        values["geometry_refs"] = [
            GeometryRef.from_dict(item) for item in values.get("geometry_refs", [])
        ]
        values["excluded_geometry_refs"] = [
            GeometryRef.from_dict(item)
            for item in values.get("excluded_geometry_refs", [])
        ]
        region = cls(**values)
        region.validate()
        return region

    def validate(self) -> None:
        if self.mode not in {
            "bbox",
            "topology_refs",
            "topology_complement",
            "whole_model",
        }:
            raise DFMError(
                "region_invalid",
                "Region selection mode is unsupported.",
                {"region_id": self.region_id, "mode": self.mode},
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256):
            raise DFMError("region_invalid", "Region input identity is invalid.")
        if self.mode == "bbox" and self.bbox is None:
            raise DFMError("region_invalid", "A bbox region requires bounds.")
        if self.mode == "topology_refs" and not self.geometry_refs:
            raise DFMError(
                "region_invalid", "A topology_refs region requires geometry refs."
            )
        if self.mode == "topology_complement" and not self.excluded_geometry_refs:
            raise DFMError(
                "region_invalid",
                "A topology_complement region requires excluded geometry refs.",
            )
        if self.mode == "whole_model" and (
            self.geometry_refs or self.excluded_geometry_refs or self.bbox is not None
        ):
            raise DFMError(
                "region_invalid", "A whole-model region cannot carry selectors."
            )
        for refs in (self.geometry_refs, self.excluded_geometry_refs):
            identities = {
                (
                    item.kind,
                    item.entity_id,
                    item.topology_snapshot_id,
                    item.input_sha256,
                )
                for item in refs
            }
            if len(identities) != len(refs) or any(
                item.kind != "face"
                or item.index < 1
                or item.input_sha256 != self.input_sha256
                or not item.topology_snapshot_id
                or not item.entity_id
                for item in refs
            ):
                raise DFMError(
                    "region_invalid",
                    "Region topology refs must be unique faces from the same input.",
                    {"region_id": self.region_id},
                )


@dataclass(frozen=True)
class GeometryRef:
    """A topology reference valid only inside one immutable topology snapshot."""

    kind: str
    index: int
    input_sha256: str = ""
    topology_snapshot_id: str = ""
    entity_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryRef":
        return cls(**payload)


@dataclass(frozen=True)
class FormingScopeRecord:
    """A reviewed direction domain inside one immutable Worker snapshot."""

    forming_scope_id: str
    snapshot_id: str
    mode: str
    draw_direction: list[float] | None
    coordinate_system: str
    direction_source: str | None
    status: str
    geometry_refs: list[GeometryRef] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    kind: str = ""
    supersedes_scope_ids: list[str] = field(default_factory=list)

    def validate(self) -> None:
        direction_required = self.mode == "main" or self.status == "confirmed"
        direction_valid = (
            isinstance(self.draw_direction, list)
            and len(self.draw_direction) == 3
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                for value in self.draw_direction
            )
            and math.sqrt(sum(float(value) ** 2 for value in self.draw_direction))
            > 1e-12
        )
        if (
            not self.forming_scope_id
            or not self.snapshot_id
            or self.mode not in {"main", "local"}
            or self.kind not in {"main_forming", "undercut", "local_forming"}
            or (self.mode == "main" and self.kind != "main_forming")
            or (self.mode == "local" and self.kind == "main_forming")
            or self.coordinate_system != "model"
            or self.status not in {"candidate", "confirmed", "stale"}
            or not isinstance(self.supersedes_scope_ids, list)
            or any(not isinstance(value, str) or not value or value == self.forming_scope_id
                   for value in self.supersedes_scope_ids)
            or len(self.supersedes_scope_ids) != len(set(self.supersedes_scope_ids))
            or (direction_required and not direction_valid)
            or (direction_required and self.direction_source != "user_confirmed")
            or (
                not direction_required
                and self.draw_direction is not None
                and not direction_valid
            )
            or (
                not direction_required
                and self.draw_direction is None
                and self.direction_source not in {None, ""}
            )
        ):
            raise DFMError(
                "forming_scope_invalid",
                "A forming scope violates the dfm.geometry/v2 direction contract.",
                {"forming_scope_id": self.forming_scope_id},
            )
        if any(
            ref.kind != "face" or ref.index < 1 or not ref.input_sha256
            for ref in self.geometry_refs
        ):
            raise DFMError(
                "forming_scope_invalid",
                "Forming scope geometry must contain valid face references.",
                {"forming_scope_id": self.forming_scope_id},
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            **asdict(self),
            "geometry_refs": [item.to_dict() for item in self.geometry_refs],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FormingScopeRecord":
        values = dict(payload)
        values["geometry_refs"] = [
            GeometryRef.from_dict(item) for item in values.get("geometry_refs", [])
        ]
        record = cls(**values)
        record.validate()
        return record


@dataclass(frozen=True)
class ReviewGroupRecord:
    """Hermes-owned user confirmation; never sent as a Worker calculation ID."""

    review_group_id: str
    kind: str
    target_refs: list[str]
    content_sha256: str
    source: str
    status: str
    created_at: str
    confirmed_at: str | None = None
    snapshot_id: str = ""

    def validate(self) -> None:
        if (
            not self.review_group_id
            or self.kind
            not in {
                "ordinary_structure",
                "surface_attribute",
                "product_forming",
                "undercut_scope",
                "local_structure",
                "local_direction",
            }
            or not self.target_refs
            or len(self.target_refs) != len(set(self.target_refs))
            or re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None
            or self.source not in {"user", "user_batch"}
            or self.status not in {"pending_review", "confirmed", "stale", "cancelled"}
        ):
            raise DFMError(
                "review_group_invalid",
                "A DFM review group has an invalid immutable identity.",
                {"review_group_id": self.review_group_id},
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReviewGroupRecord":
        record = cls(**payload)
        record.validate()
        return record


@dataclass(frozen=True)
class GeometricResultRecord:
    """One public dfm.geometry/v2 result, without legacy Worker routing fields."""

    result_id: str
    snapshot_id: str
    feature_id: str
    geometric_id: str
    status: str
    value: Any
    unit: str | None
    context: dict[str, Any]
    evidence: dict[str, Any]
    quality: dict[str, Any] | None
    reason: str | None
    provenance: dict[str, Any]
    input_sha256: str

    @property
    def measurement_id(self) -> str:
        return self.result_id

    @property
    def feature_refs(self) -> list[str]:
        return [self.feature_id]

    @property
    def region_refs(self) -> list[str]:
        return []

    @property
    def geometry_refs(self) -> list[GeometryRef]:
        refs = self.evidence.get("geometry_refs", [])
        return [GeometryRef.from_dict(item) for item in refs if isinstance(item, dict)]

    @property
    def field_refs(self) -> list[str]:
        refs = self.evidence.get("field_refs", [])
        return [str(item) for item in refs if isinstance(item, str)]

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {}

    def validate(self) -> None:
        if (
            not self.result_id
            or not self.snapshot_id
            or not self.feature_id
            or re.fullmatch(r"G_[A-Z0-9_]+", self.geometric_id) is None
            or self.status
            not in {
                "ok",
                "unsupported",
                "not_applicable",
                "missing_context",
                "ambiguous_scope",
                "failed",
            }
            or not isinstance(self.context, dict)
            or not isinstance(self.evidence, dict)
            or not isinstance(self.provenance, dict)
            or not isinstance(self.provenance.get("plan_fragment_ref"), str)
            or not self.provenance["plan_fragment_ref"]
            or re.fullmatch(r"[0-9a-f]{64}", self.input_sha256) is None
        ):
            raise DFMError(
                "geometric_result_invalid",
                "A Worker Geometric result violates dfm.geometry/v2.",
                {"result_id": self.result_id},
            )
        if self.status == "ok" and self.value is None:
            raise DFMError(
                "geometric_result_invalid",
                "A successful Geometric result must contain a value.",
                {"result_id": self.result_id},
            )
        if self.status != "ok" and self.value is not None:
            raise DFMError(
                "geometric_result_invalid",
                "An unavailable Geometric result cannot contain a fabricated value.",
                {"result_id": self.result_id},
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricResultRecord":
        result = cls(**payload)
        result.validate()
        return result


@dataclass(frozen=True)
class MeasurementRecord:
    """Deterministic geometry output, intentionally independent of a rule verdict."""

    measurement_id: str
    operation_id: str
    calculator_id: str
    metric_id: str
    quantity_id: str
    value: Any
    unit: str | None
    status: str
    geometry_refs: list[GeometryRef]
    method: str
    algorithm_version: str
    input_sha256: str
    quality: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    region_refs: list[str] = field(default_factory=list)
    field_refs: list[str] = field(default_factory=list)
    feature_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "geometry_refs": [item.to_dict() for item in self.geometry_refs],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MeasurementRecord":
        values = dict(payload)
        values["geometry_refs"] = [
            GeometryRef.from_dict(item) for item in values.get("geometry_refs", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class EvaluationRecord:
    """Legacy non-v2 comparison retained for explicit old backends."""

    evaluation_id: str
    operation_id: str
    metric_id: str
    measurement_ids: list[str]
    rule_id: str
    rule_version: str
    rule_hash: str
    operator: str
    expected: Any
    actual: Any
    outcome: str
    feature_refs: list[str] = field(default_factory=list)
    region_refs: list[str] = field(default_factory=list)
    check_id: str = ""
    actual_unit: str | None = None
    expression: dict[str, Any] | None = None
    operand_values: dict[str, Any] = field(default_factory=dict)
    severity: str = "unclassified"
    severity_rationale: str | None = None
    criterion_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvaluationRecord":
        return cls(**payload)


@dataclass(frozen=True)
class GeometricEvaluationRecord:
    """Rule verdict over published G results, without legacy routing identity."""

    evaluation_id: str
    geometric_id: str
    measurement_ids: list[str]
    rule_id: str
    rule_version: str
    rule_hash: str
    operator: str
    expected: Any
    actual: Any
    outcome: str
    feature_refs: list[str] = field(default_factory=list)
    check_id: str = ""
    actual_unit: str | None = None
    expression: dict[str, Any] | None = None
    operand_values: dict[str, Any] = field(default_factory=dict)
    severity: str = "unclassified"
    severity_rationale: str | None = None
    criterion_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricEvaluationRecord":
        return cls(**payload)


@dataclass(frozen=True)
class EvidenceRecord:
    """Legacy evidence binding retained for explicit old backends."""

    evidence_id: str
    run_id: str
    input_sha256: str
    operation_id: str
    metric_id: str
    measurement_ids: list[str]
    evaluation_ids: list[str]
    geometry_refs: list[GeometryRef]
    region_refs: list[str]
    artifact_ref: str
    render: dict[str, Any]
    feature_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "geometry_refs": [item.to_dict() for item in self.geometry_refs],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvidenceRecord":
        values = dict(payload)
        values["geometry_refs"] = [
            GeometryRef.from_dict(item) for item in values.get("geometry_refs", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class GeometricEvidenceRecord:
    """Evidence for a G evaluation, scoped by Feature and geometry references."""

    evidence_id: str
    run_id: str
    input_sha256: str
    geometric_id: str
    measurement_ids: list[str]
    evaluation_ids: list[str]
    geometry_refs: list[GeometryRef]
    artifact_ref: str
    render: dict[str, Any]
    feature_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "geometry_refs": [item.to_dict() for item in self.geometry_refs],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricEvidenceRecord":
        values = dict(payload)
        values["geometry_refs"] = [
            GeometryRef.from_dict(item) for item in values.get("geometry_refs", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class GeometryQuery:
    """One feature-scoped calculation resolved from a frozen Discovery snapshot."""

    snapshot_id: str
    feature_id: str
    geometric_ids: list[str]

    def __post_init__(self) -> None:
        if (
            not self.snapshot_id
            or not self.feature_id
            or not self.geometric_ids
            or len(self.geometric_ids) != len(set(self.geometric_ids))
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"G_[A-Z0-9_]+", value) is None
                for value in self.geometric_ids
            )
        ):
            raise ValueError("Geometry query identity is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryQuery":
        return cls(**payload)


@dataclass(frozen=True)
class ObjectiveTaskRequest:
    """Backend-neutral objective geometry task; contains no rules or presentation policy."""

    schema_version: int
    run_id: str
    input_sha256: str
    input_format: str
    process: str
    scope_id: str
    scope_version: str
    operations: list[PlanOperation] = field(default_factory=list)
    regions: list[RegionRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (
            self.schema_version != OBJECTIVE_SCHEMA_VERSION
            or not self.run_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256)
            or not self.input_format
            or not self.process
            or not self.scope_id
            or not self.scope_version
            or not self.operations
        ):
            raise ValueError("Objective task identity is invalid.")
        for operation in self.operations:
            operation.validate()
        regions = {item.region_id: item for item in self.regions}
        if len(regions) != len(self.regions):
            raise ValueError("Objective task region identities are not unique.")
        for operation in self.operations:
            if set(operation.region_refs) - set(regions):
                raise ValueError(
                    "Objective task operation has unresolved region references."
                )
        for region in self.regions:
            region.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "operations": [operation.to_dict() for operation in self.operations],
            "regions": [region.to_dict() for region in self.regions],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ObjectiveTaskRequest":
        values = dict(payload)
        values["operations"] = [
            PlanOperation.from_dict(value) for value in values.get("operations", [])
        ]
        values["regions"] = [
            RegionRecord.from_dict(value) for value in values.get("regions", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class LocalObjectiveWorkerRequest:
    """Local process envelope around the same task sent to a remote backend."""

    schema_version: int
    backend_version: str
    input_path: str
    output_dir: str
    task: ObjectiveTaskRequest | GeometryQuery
    contract_version: str = ""
    snapshot_path: str = ""

    def __post_init__(self) -> None:
        if (
            self.schema_version != WORKER_SCHEMA_VERSION
            or not self.backend_version
            or not self.input_path
            or not self.output_dir
            or self.contract_version not in {"", GEOMETRY_REQUEST_CONTRACT}
            or isinstance(self.task, GeometryQuery) != bool(self.snapshot_path)
        ):
            raise ValueError("Local objective worker envelope is invalid.")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "backend_version": self.backend_version,
            "input_path": self.input_path,
            "output_dir": self.output_dir,
            "task": self.task.to_dict(),
        }
        if self.contract_version:
            payload["contract_version"] = self.contract_version
        if self.snapshot_path:
            payload["snapshot_path"] = self.snapshot_path
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LocalObjectiveWorkerRequest":
        values = dict(payload)
        task = values["task"]
        values["task"] = (
            GeometryQuery.from_dict(task)
            if values.get("snapshot_path")
            else ObjectiveTaskRequest.from_dict(task)
        )
        return cls(**values)


@dataclass(frozen=True)
class WorkerEvent:
    schema_version: int
    type: str
    stage: str | None = None
    percent: int | None = None
    processed_faces: int | None = None
    total_faces: int | None = None
    elapsed_seconds: float | None = None
    kind: str | None = None
    path: str | None = None
    code: str | None = None
    message: str | None = None
    external_job_id: str | None = None
    contract_version: str = ""
    measured_feature_count: int | None = None
    unavailable_features: list[dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not self.contract_version:
            payload.pop("contract_version")
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkerEvent":
        try:
            event = cls(**payload)
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "worker_event_invalid", "DFM worker event is invalid."
            ) from exc
        if event.schema_version != WORKER_SCHEMA_VERSION:
            raise DFMError(
                "worker_event_invalid",
                "DFM worker event schema version is unsupported.",
                {"schema_version": event.schema_version},
            )
        if event.contract_version not in {"", GEOMETRY_EVENT_CONTRACT}:
            raise DFMError(
                "worker_event_invalid",
                "DFM worker event contract version is unsupported.",
                {"contract_version": event.contract_version},
            )
        if event.type not in {"progress", "artifact", "completed", "error"}:
            raise DFMError(
                "worker_event_invalid",
                "DFM worker event type is unsupported.",
                {"type": event.type},
            )
        if event.type == "progress" and (
            event.percent is None or not 0 <= event.percent <= 100
        ):
            raise DFMError(
                "worker_event_invalid",
                "DFM worker progress percent must be between 0 and 100.",
            )
        if event.type == "progress" and (
            (event.processed_faces is None) != (event.total_faces is None)
            or event.processed_faces is not None
            and (
                event.processed_faces < 0
                or event.total_faces is None
                or event.total_faces < event.processed_faces
            )
            or event.elapsed_seconds is not None
            and event.elapsed_seconds < 0
        ):
            raise DFMError(
                "worker_event_invalid",
                "DFM worker progress details are invalid.",
            )
        if event.measured_feature_count is not None and (
            event.type != "progress"
            or isinstance(event.measured_feature_count, bool)
            or not isinstance(event.measured_feature_count, int)
            or event.measured_feature_count < 0
        ):
            raise DFMError(
                "worker_event_invalid", "DFM measured feature count is invalid."
            )
        if event.unavailable_features is not None and (
            event.type != "progress"
            or not isinstance(event.unavailable_features, list)
            or any(
                not isinstance(item, dict)
                or not all(
                    isinstance(item.get(key), str) and item[key]
                    for key in ("feature_id", "quantity_id", "status", "reason")
                )
                or item["status"] not in {"not_supported", "not_applicable"}
                for item in event.unavailable_features
            )
        ):
            raise DFMError(
                "worker_event_invalid", "DFM unavailable features are invalid."
            )
        return event


@dataclass(frozen=True)
class ObjectiveArtifactManifest:
    artifact_id: str
    kind: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.filename)
        if (
            not self.artifact_id
            or not self.kind
            or not self.filename
            or path.is_absolute()
            or ".." in path.parts
            or not self.media_type
            or self.size_bytes < 0
            or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
        ):
            raise ValueError("Objective artifact manifest is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ObjectiveArtifactManifest":
        return cls(**payload)


@dataclass(frozen=True)
class GeometryDiscoveryTaskRequest:
    """Backend-neutral request for geometry discovery before analysis planning."""

    schema_version: int
    request_id: str
    input_id: str
    input_sha256: str
    input_format: str
    process: str
    recognizer_ids: list[str] = field(default_factory=list)
    facts: dict[str, ResolvedArgument] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.schema_version != DISCOVERY_SCHEMA_VERSION
            or not self.request_id
            or not self.input_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256)
            or not self.input_format
            or not self.process
            or not self.recognizer_ids
            or len(self.recognizer_ids) != len(set(self.recognizer_ids))
        ):
            raise ValueError("Geometry discovery task identity is invalid.")
        for name, fact in self.facts.items():
            if (
                not name
                or not isinstance(fact, ResolvedArgument)
                or not fact.source_ref
            ):
                raise ValueError("Geometry discovery facts require stable provenance.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "input_id": self.input_id,
            "input_sha256": self.input_sha256,
            "input_format": self.input_format,
            "process": self.process,
            "recognizer_ids": list(self.recognizer_ids),
            "facts": {key: value.to_dict() for key, value in self.facts.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryDiscoveryTaskRequest":
        values = dict(payload)
        values["facts"] = {
            key: ResolvedArgument.from_dict(value)
            for key, value in values.get("facts", {}).items()
        }
        return cls(**values)


@dataclass(frozen=True)
class RecognizerExecutionResult:
    """Per-recognizer status so partial discovery never becomes a fake feature."""

    recognizer_id: str
    status: str
    implementation_version: str = ""
    feature_refs: list[str] = field(default_factory=list)
    region_refs: list[str] = field(default_factory=list)
    observation_refs: list[str] = field(default_factory=list)
    missing_fact_names: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.recognizer_id or self.status not in {
            "completed",
            "blocked",
            "not_implemented",
            "failed",
        }:
            raise ValueError("Geometry recognizer result is invalid.")
        for refs in (
            self.feature_refs,
            self.region_refs,
            self.observation_refs,
            self.missing_fact_names,
        ):
            if len(refs) != len(set(refs)) or any(not item for item in refs):
                raise ValueError("Geometry recognizer references must be unique.")
        if self.status == "blocked" and not self.missing_fact_names:
            raise ValueError("A blocked geometry recognizer must name missing facts.")
        if self.status != "blocked" and self.missing_fact_names:
            raise ValueError(
                "Only a blocked geometry recognizer may name missing facts."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RecognizerExecutionResult":
        return cls(**payload)


@dataclass(frozen=True)
class GeometryDiscoveryResultManifest:
    """Immutable OCCT discovery output consumed before Hermes compiles rules."""

    schema_version: int
    producer_version: str
    request_id: str
    input_id: str
    input_sha256: str
    process: str
    topology_snapshot_id: str
    render_mesh_snapshot_id: str
    geometry_snapshot_ref: str
    observations: list[ObservationRecord] = field(default_factory=list)
    features: list[FeatureRecord] = field(default_factory=list)
    regions: list[RegionRecord] = field(default_factory=list)
    recognizers: list[RecognizerExecutionResult] = field(default_factory=list)
    artifacts: list[ObjectiveArtifactManifest] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.schema_version != DISCOVERY_SCHEMA_VERSION
            or not self.producer_version
            or not self.request_id
            or not self.input_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256)
            or not self.process
            or not self.topology_snapshot_id
            or not self.render_mesh_snapshot_id
            or not self.geometry_snapshot_ref
            or not self.recognizers
            or not self.artifacts
        ):
            raise ValueError("Geometry discovery result identity is invalid.")

        feature_ids = {item.feature_id for item in self.features}
        region_ids = {item.region_id for item in self.regions}
        observation_ids = {item.observation_id for item in self.observations}
        if (
            len(feature_ids) != len(self.features)
            or len(region_ids) != len(self.regions)
            or len(observation_ids) != len(self.observations)
        ):
            raise ValueError("Geometry discovery output identities must be unique.")

        for observation in self.observations:
            if (
                observation.input_id != self.input_id
                or set(observation.feature_refs) - feature_ids
                or set(observation.region_refs) - region_ids
            ):
                raise ValueError(
                    "Geometry discovery observation references are invalid."
                )
        for feature in self.features:
            if (
                feature.input_sha256 != self.input_sha256
                or not feature.region_refs
                or set(feature.region_refs) - region_ids
            ):
                raise ValueError("Geometry discovery feature references are invalid.")
        for region in self.regions:
            if (
                region.input_sha256 != self.input_sha256
                or set(region.feature_refs) - feature_ids
            ):
                raise ValueError("Geometry discovery region references are invalid.")
            for geometry_ref in [
                *region.geometry_refs,
                *region.excluded_geometry_refs,
            ]:
                if (
                    geometry_ref.input_sha256 != self.input_sha256
                    or geometry_ref.topology_snapshot_id != self.topology_snapshot_id
                ):
                    raise ValueError(
                        "Geometry discovery topology reference belongs to another snapshot."
                    )

        recognizer_ids = [item.recognizer_id for item in self.recognizers]
        if len(recognizer_ids) != len(set(recognizer_ids)):
            raise ValueError("Geometry discovery recognizer identities must be unique.")
        for recognizer in self.recognizers:
            if (
                set(recognizer.feature_refs) - feature_ids
                or set(recognizer.region_refs) - region_ids
                or set(recognizer.observation_refs) - observation_ids
            ):
                raise ValueError(
                    "Geometry recognizer output references are unresolved."
                )

        artifact_by_id = {item.artifact_id: item for item in self.artifacts}
        artifact_ids = set(artifact_by_id)
        filenames = {item.filename for item in self.artifacts}
        required_kinds = {"geometry_snapshot", "topology_map", "render_scene"}
        kind_counts = {
            kind: sum(item.kind == kind for item in self.artifacts)
            for kind in required_kinds
        }
        if (
            len(artifact_ids) != len(self.artifacts)
            or len(filenames) != len(self.artifacts)
            or self.geometry_snapshot_ref not in artifact_ids
            or artifact_by_id[self.geometry_snapshot_ref].kind != "geometry_snapshot"
            or any(count != 1 for count in kind_counts.values())
        ):
            raise ValueError(
                "Geometry discovery artifacts are incomplete or ambiguous."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "producer_version": self.producer_version,
            "request_id": self.request_id,
            "input_id": self.input_id,
            "input_sha256": self.input_sha256,
            "process": self.process,
            "topology_snapshot_id": self.topology_snapshot_id,
            "render_mesh_snapshot_id": self.render_mesh_snapshot_id,
            "geometry_snapshot_ref": self.geometry_snapshot_ref,
            "observations": [item.to_dict() for item in self.observations],
            "features": [item.to_dict() for item in self.features],
            "regions": [item.to_dict() for item in self.regions],
            "recognizers": [item.to_dict() for item in self.recognizers],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryDiscoveryResultManifest":
        values = dict(payload)
        values["observations"] = [
            ObservationRecord.from_dict(item) for item in values.get("observations", [])
        ]
        values["features"] = [
            FeatureRecord.from_dict(item) for item in values.get("features", [])
        ]
        values["regions"] = [
            RegionRecord.from_dict(item) for item in values.get("regions", [])
        ]
        values["recognizers"] = [
            RecognizerExecutionResult.from_dict(item)
            for item in values.get("recognizers", [])
        ]
        values["artifacts"] = [
            ObjectiveArtifactManifest.from_dict(item)
            for item in values.get("artifacts", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class ObjectiveResultManifest:
    schema_version: int
    producer_version: str
    run_id: str
    input_sha256: str
    process: str
    scope_id: str
    scope_version: str
    result_path: str
    artifacts: list[ObjectiveArtifactManifest] = field(default_factory=list)
    contract_version: str = ""

    def __post_init__(self) -> None:
        if (
            self.schema_version != OBJECTIVE_SCHEMA_VERSION
            or not self.producer_version
            or not self.run_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256)
            or not self.process
            or not self.scope_id
            or not self.scope_version
            or not self.result_path
            or not self.artifacts
            or self.contract_version not in {"", GEOMETRY_RESULT_CONTRACT}
        ):
            raise ValueError("Objective result manifest identity is invalid.")
        artifact_ids = [item.artifact_id for item in self.artifacts]
        filenames = [item.filename for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)) or len(filenames) != len(
            set(filenames)
        ):
            raise ValueError("Objective result artifacts must be unique.")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            **asdict(self),
            "artifacts": [item.to_dict() for item in self.artifacts],
        }
        if not self.contract_version:
            payload.pop("contract_version")
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ObjectiveResultManifest":
        values = dict(payload)
        values["artifacts"] = [
            ObjectiveArtifactManifest.from_dict(item)
            for item in values.get("artifacts", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class FindingRecord:
    """Legacy finding retained for explicit old backends."""

    finding_id: str
    title: str
    severity: str
    status: str
    evaluation_ids: list[str]
    measurement_ids: list[str]
    metric_ids: list[str]
    region_refs: list[str]
    evidence_refs: list[str]
    rule_refs: list[str]
    recommendation: str
    feature_refs: list[str] = field(default_factory=list)
    check_ids: list[str] = field(default_factory=list)
    severity_rationale: str | None = None
    criterion_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FindingRecord":
        return cls(**payload)


@dataclass(frozen=True)
class GeometricFindingRecord:
    """Published issue derived only from Check, Feature, G, rule and evidence IDs."""

    finding_id: str
    title: str
    severity: str
    status: str
    evaluation_ids: list[str]
    measurement_ids: list[str]
    geometric_ids: list[str]
    evidence_refs: list[str]
    rule_refs: list[str]
    recommendation: str
    feature_refs: list[str] = field(default_factory=list)
    check_ids: list[str] = field(default_factory=list)
    contexts: list[dict[str, Any]] = field(default_factory=list)
    severity_rationale: str | None = None
    criterion_results: list[dict[str, Any]] = field(default_factory=list)
    validity_status: str = "current"
    stale_reason: str | None = None
    location_status: str = "preliminary"
    plan_fragment_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometricFindingRecord":
        return cls(**payload)


@dataclass(frozen=True)
class InputRecord:
    input_id: str
    kind: str
    source_name: str
    relative_path: str
    size_bytes: int
    sha256: str
    created_at: str
    preflight: dict[str, Any] = field(default_factory=dict)
    supersedes_input_id: str | None = None
    format_id: str = ""
    representation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "kind": self.kind,
            "source_name": self.source_name,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "preflight": dict(self.preflight),
            "supersedes_input_id": self.supersedes_input_id,
            "format_id": self.format_id,
            "representation": self.representation,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InputRecord":
        return cls(**payload)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    relative_path: str
    media_type: str
    created_at: str
    run_id: str = ""
    logical_id: str = ""
    size_bytes: int = 0
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "logical_id": self.logical_id or self.artifact_id,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ArtifactRecord":
        return cls(**payload)


def validate_result_manifest_mesh_binding(
    result_manifest: dict[str, Any],
    scene: dict[str, Any],
    topology: dict[str, Any],
    *,
    scene_artifact_id: str,
    topology_artifact_id: str,
) -> tuple[str, str]:
    """Verify that a report/viewer consumes the mesh version pinned by its run."""

    render_snapshot = scene.get("render_mesh_snapshot")
    topology_snapshot = topology.get("topology_snapshot")
    render_snapshot_id = (
        str(render_snapshot.get("render_mesh_snapshot_id") or "")
        if isinstance(render_snapshot, dict)
        else ""
    )
    topology_snapshot_id = (
        str(topology_snapshot.get("topology_snapshot_id") or "")
        if isinstance(topology_snapshot, dict)
        else ""
    )
    mesh_refs = result_manifest.get("mesh_refs")
    valid = (
        isinstance(mesh_refs, list)
        and {scene_artifact_id, topology_artifact_id}.issubset(
            {str(value) for value in mesh_refs}
        )
        and render_snapshot_id
        and topology_snapshot_id
        and result_manifest.get("render_mesh_snapshot_id") == render_snapshot_id
        and result_manifest.get("topology_snapshot_id") == topology_snapshot_id
        and topology.get("render_mesh_snapshot_ref") == render_snapshot_id
    )
    if not valid:
        raise DFMError(
            "result_manifest_invalid",
            "The result manifest does not pin the supplied scene and topology versions.",
            {
                "render_mesh_snapshot_id": render_snapshot_id or None,
                "topology_snapshot_id": topology_snapshot_id or None,
            },
        )
    return render_snapshot_id, topology_snapshot_id


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    analyzer_key: str
    analyzer_version: str
    status: RunStatus
    created_at: str
    updated_at: str
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    error: dict[str, Any] | None = None
    idempotency_key: str | None = None
    owner_pid: int | None = None
    runtime_id: str | None = None
    plan_id: str | None = None
    plan_snapshot: dict[str, Any] | None = None
    stage: str | None = None
    progress_percent: int = 0
    processed_faces: int | None = None
    total_faces: int | None = None
    elapsed_seconds: float | None = None
    heartbeat_at: str | None = None
    event_log_path: str | None = None
    worker_stdout_path: str | None = None
    worker_stderr_path: str | None = None
    external_job_id: str | None = None
    completed_fragment_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "analyzer_key": self.analyzer_key,
            "analyzer_version": self.analyzer_version,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "error": self.error,
            "idempotency_key": self.idempotency_key,
            "owner_pid": self.owner_pid,
            "runtime_id": self.runtime_id,
            "plan_id": self.plan_id,
            "plan_snapshot": self.plan_snapshot,
            "stage": self.stage,
            "progress_percent": self.progress_percent,
            "processed_faces": self.processed_faces,
            "total_faces": self.total_faces,
            "elapsed_seconds": self.elapsed_seconds,
            "heartbeat_at": self.heartbeat_at,
            "event_log_path": self.event_log_path,
            "worker_stdout_path": self.worker_stdout_path,
            "worker_stderr_path": self.worker_stderr_path,
            "external_job_id": self.external_job_id,
            "completed_fragment_refs": list(self.completed_fragment_refs),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunRecord":
        values = dict(payload)
        values["status"] = RunStatus(values["status"])
        values["artifacts"] = [
            ArtifactRecord.from_dict(item) for item in values.get("artifacts", [])
        ]
        return cls(**values)


@dataclass(frozen=True)
class ProjectManifest:
    project_id: str
    name: str
    created_at: str
    updated_at: str
    domain: str = "injection_molding"
    process: str = "injection"
    process_source: str = "default"
    input_mode: str | None = None
    inputs: list[InputRecord] = field(default_factory=list)
    facts: list[FactRecord] = field(default_factory=list)
    regions: list[RegionRecord] = field(default_factory=list)
    clarifications: list[ClarificationRecord] = field(default_factory=list)
    features: list[FeatureRecord] = field(default_factory=list)
    observations: list[ObservationRecord] = field(default_factory=list)
    fusion_links: list[FusionLinkRecord] = field(default_factory=list)
    discovery_snapshots: list[DiscoverySnapshotRecord] = field(default_factory=list)
    forming_scopes: list[FormingScopeRecord] = field(default_factory=list)
    review_groups: list[ReviewGroupRecord] = field(default_factory=list)
    geometry_edits: list[dict[str, Any]] = field(default_factory=list)
    plans: list[PlanRecord] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    findings: list[FindingRecord | GeometricFindingRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION
    revision: int = 0
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "domain": self.domain,
            "process": self.process,
            "process_source": self.process_source,
            "input_mode": self.input_mode,
            "revision": self.revision,
            "idempotency_key": self.idempotency_key,
            "inputs": [item.to_dict() for item in self.inputs],
            "facts": [item.to_dict() for item in self.facts],
            "regions": [item.to_dict() for item in self.regions],
            "clarifications": [item.to_dict() for item in self.clarifications],
            "features": [item.to_dict() for item in self.features],
            "observations": [item.to_dict() for item in self.observations],
            "fusion_links": [item.to_dict() for item in self.fusion_links],
            "discovery_snapshots": [
                item.to_dict() for item in self.discovery_snapshots
            ],
            "forming_scopes": [item.to_dict() for item in self.forming_scopes],
            "review_groups": [item.to_dict() for item in self.review_groups],
            "geometry_edits": [dict(item) for item in self.geometry_edits],
            "plans": [item.to_dict() for item in self.plans],
            "runs": [run.to_dict() for run in self.runs],
            "findings": [item.to_dict() for item in self.findings],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "capabilities": self.capabilities,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectManifest":
        values = dict(payload)
        values["inputs"] = [
            InputRecord.from_dict(item) for item in values.get("inputs", [])
        ]
        values["facts"] = [
            FactRecord.from_dict(item) for item in values.get("facts", [])
        ]
        values["regions"] = [
            RegionRecord.from_dict(item) for item in values.get("regions", [])
        ]
        values["clarifications"] = [
            ClarificationRecord.from_dict(item)
            for item in values.get("clarifications", [])
        ]
        values["features"] = [
            FeatureRecord.from_dict(item) for item in values.get("features", [])
        ]
        values["observations"] = [
            ObservationRecord.from_dict(item) for item in values.get("observations", [])
        ]
        values["fusion_links"] = [
            FusionLinkRecord.from_dict(item) for item in values.get("fusion_links", [])
        ]
        values["discovery_snapshots"] = [
            DiscoverySnapshotRecord.from_dict(item)
            for item in values.get("discovery_snapshots", [])
        ]
        values["forming_scopes"] = [
            FormingScopeRecord.from_dict(item)
            for item in values.get("forming_scopes", [])
        ]
        values["review_groups"] = [
            ReviewGroupRecord.from_dict(item)
            for item in values.get("review_groups", [])
        ]
        values["plans"] = [
            PlanRecord.from_dict(item) for item in values.get("plans", [])
        ]
        values["runs"] = [RunRecord.from_dict(item) for item in values.get("runs", [])]
        values["findings"] = [
            (
                GeometricFindingRecord.from_dict(item)
                if "geometric_ids" in item
                else FindingRecord.from_dict(item)
            )
            for item in values.get("findings", [])
        ]
        values["artifacts"] = [
            ArtifactRecord.from_dict(item) for item in values.get("artifacts", [])
        ]
        return cls(**values)
