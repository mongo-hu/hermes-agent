"""Build physical evaluation cases before rule selection and comparison."""

from dataclasses import dataclass
from collections.abc import Iterator

from ..contracts import (
    GeometricResultRecord,
    GeometricRuleBinding,
    GeometricRuleOperand,
    MeasurementRecord,
    PlanRecord,
    RuleBinding,
)
from ..runtime.geometric_batches import request_key


def geometric_operand_matches(
    measurements: list[MeasurementRecord | GeometricResultRecord],
    plan: PlanRecord,
    binding: GeometricRuleBinding,
    operand: GeometricRuleOperand,
) -> list[GeometricResultRecord]:
    context = request_key("", "", operand.context)[2]
    fragments = {
        fragment.fragment_id
        for fragment in plan.active_plan_fragments(
            statuses={"ready", "running", "completed", "needs_attention"}
        )
        if (
            not fragment.rule_binding_refs
            or binding.binding_id in fragment.rule_binding_refs
        )
        and any(
            (not operand.feature_refs or item.feature_id in operand.feature_refs)
            and operand.geometric_id in item.geometric_ids
            and request_key("", "", item.context)[2] == context
            for item in fragment.items
        )
    }
    return sorted(
        (
            result
            for result in measurements
            if isinstance(result, GeometricResultRecord)
            and result.geometric_id == operand.geometric_id
            and request_key("", "", result.context)[2] == context
            and set(operand.feature_refs).issubset(result.feature_refs)
            and (
                not fragments or result.provenance.get("plan_fragment_ref") in fragments
            )
        ),
        key=lambda item: item.measurement_id,
    )


def rule_case_key(binding: RuleBinding | GeometricRuleBinding) -> tuple:
    if binding.rule_selection is None:
        return ("direct", binding.binding_id)
    return (
        "selected",
        binding.check_id,
        tuple(sorted(binding.feature_refs)),
        request_key("", "", binding.context)[2]
        if isinstance(binding, GeometricRuleBinding)
        else tuple(sorted(binding.region_refs)),
    )


@dataclass(frozen=True)
class EvaluationCase:
    bindings: list[RuleBinding | GeometricRuleBinding]
    measurements: list[MeasurementRecord | GeometricResultRecord]
    instance_id: str | None = None


def evaluation_cases(
    measurements: list[MeasurementRecord | GeometricResultRecord],
    plan: PlanRecord,
) -> Iterator[EvaluationCase]:
    groups = {}
    for binding in plan.rule_bindings:
        groups.setdefault(rule_case_key(binding), []).append(binding)
    for bindings in groups.values():
        identity_results = {}
        for binding in bindings:
            if not isinstance(binding, GeometricRuleBinding):
                continue
            for operand in binding.measurement_operands():
                if operand.aggregation != "identity":
                    continue
                for result in geometric_operand_matches(
                    measurements, plan, binding, operand
                ):
                    identity_results[result.result_id] = result
        instances = {
            result.measurement_instance_id
            for result in identity_results.values()
            if result.measurement_instance_id is not None
        }
        if not instances:
            yield EvaluationCase(bindings, measurements)
            continue
        # Keep explicit aggregate inputs as a complete series. When the same
        # quantity is also consumed with identity, resolution applies the case
        # filter only to that operand.
        for instance in sorted(instances):
            yield EvaluationCase(bindings, measurements, instance)
