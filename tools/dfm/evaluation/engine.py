"""Evaluate persisted measurements against a persisted DFM plan in Hermes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import operator
from pathlib import Path
import statistics
from typing import Any

from ..contracts import (
    ArtifactRecord,
    EvaluationRecord,
    MeasurementRecord,
    PlanRecord,
    RuleBinding,
    RuleOperand,
)
from ..errors import DFMError


EVALUATION_SCHEMA_VERSION = 2
_OPERATORS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}
_DIMENSIONLESS_UNITS = {None, "", "1", "ratio"}


@dataclass(frozen=True)
class _ExpressionValue:
    value: Any
    unit: str | None


@dataclass(frozen=True)
class _ResolvedOperand:
    alias: str
    value: Any
    unit: str | None
    aggregation: str
    measurements: tuple[MeasurementRecord, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EvaluationEngine:
    """The sole production owner of Measurement → Evaluation comparison."""

    version = "hermes-evaluation-v2"

    def materialize(
        self,
        project_dir: Path,
        run_id: str,
        plan: PlanRecord,
        artifacts: list[ArtifactRecord],
    ) -> ArtifactRecord:
        measurement_artifact = next(
            (item for item in artifacts if item.kind == "measurements"), None
        )
        if measurement_artifact is None:
            raise DFMError(
                "measurements_invalid",
                "A successful geometry run must provide measurements for evaluation.",
            )
        try:
            payload = json.loads(
                (project_dir / measurement_artifact.relative_path).read_text(
                    encoding="utf-8"
                )
            )
            measurements = [
                MeasurementRecord.from_dict(item)
                for item in payload.get("measurements", [])
            ]
        except (OSError, TypeError, ValueError) as exc:
            raise DFMError(
                "measurements_invalid",
                "The measurements artifact cannot be evaluated.",
            ) from exc
        evaluations, provenance = self.evaluate(measurements, plan)
        output_dir = project_dir / "runs" / run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "evaluations.json"
        output_path.write_text(
            json.dumps(
                {
                    "schema_version": EVALUATION_SCHEMA_VERSION,
                    "engine_version": self.version,
                    "run_id": run_id,
                    "input_sha256": str(payload.get("input_sha256") or ""),
                    "process": plan.process,
                    "scope_id": plan.scope_id,
                    "scope_version": plan.scope_version,
                    "measurement_artifact": measurement_artifact.relative_path,
                    "evaluations": [item.to_dict() for item in evaluations],
                    "provenance": provenance,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return ArtifactRecord(
            f"artifact_{run_id}_evaluations",
            "evaluations",
            output_path.relative_to(project_dir).as_posix(),
            "application/json",
            _utc_now(),
        )

    def evaluate(
        self, measurements: list[MeasurementRecord], plan: PlanRecord
    ) -> tuple[list[EvaluationRecord], dict[str, dict[str, Any]]]:
        results: list[EvaluationRecord] = []
        provenance: dict[str, dict[str, Any]] = {}
        if plan.rule_bindings:
            for binding in plan.rule_bindings:
                evaluation, source = self._evaluate_binding(measurements, plan, binding)
                results.append(evaluation)
                provenance[evaluation.evaluation_id] = source
            return results, provenance

        for measurement in measurements:
            spec = self._legacy_spec(measurement, plan)
            if spec is None:
                continue
            evaluation, source = self._evaluate_legacy_measurement(
                measurement, plan, spec
            )
            results.append(evaluation)
            provenance[evaluation.evaluation_id] = source
        return results, provenance

    def _evaluate_binding(
        self,
        measurements: list[MeasurementRecord],
        plan: PlanRecord,
        binding: RuleBinding,
    ) -> tuple[EvaluationRecord, dict[str, Any]]:
        binding.validate()
        parameter = plan.rules.get(binding.rule_id)
        if parameter is None:
            raise DFMError(
                "evaluation_rule_missing",
                "A bound production rule is absent from the effective rule set.",
                {"binding_id": binding.binding_id, "rule_id": binding.rule_id},
            )
        if parameter.value is None:
            raise DFMError(
                "evaluation_rule_missing",
                "No effective threshold exists for a bound engineering rule.",
                {"binding_id": binding.binding_id, "rule_id": binding.rule_id},
            )

        resolved = {
            operand.alias: self._resolve_operand(measurements, plan, binding, operand)
            for operand in binding.measurement_operands()
        }
        linked = [
            measurement
            for operand in resolved.values()
            for measurement in operand.measurements
        ]
        input_hashes = {item.input_sha256 for item in linked}
        if len(input_hashes) != 1:
            raise DFMError(
                "evaluation_operand_invalid",
                "A rule expression cannot combine Measurements from different inputs.",
                {
                    "binding_id": binding.binding_id,
                    "input_sha256": sorted(input_hashes),
                },
            )

        expression = binding.expression or {"operand": binding.operand_alias}
        actual = self._evaluate_expression(expression, resolved, binding.binding_id)
        self._validate_result_unit(
            actual.unit, parameter.unit, binding_id=binding.binding_id
        )
        passed = self._compare(
            binding.operator,
            actual.value,
            parameter.value,
            binding_id=binding.binding_id,
        )
        measurement_ids = list(dict.fromkeys(item.measurement_id for item in linked))
        rule_version = parameter.version
        rule_hash = self._rule_hash(
            binding=binding,
            rule_version=rule_version,
            expected=parameter.value,
            expected_unit=parameter.unit,
        )
        if binding.expression is None and not binding.additional_operands:
            primary = linked[0]
            stable_id = str(
                primary.diagnostics.get("legacy_issue_id") or primary.measurement_id
            ).lower()
        else:
            stable_id = hashlib.sha256(
                json.dumps(
                    {
                        "binding_id": binding.binding_id,
                        "measurement_ids": measurement_ids,
                        "rule_hash": rule_hash,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
        operand_values = {
            alias: {
                "value": item.value,
                "unit": item.unit,
                "aggregation": item.aggregation,
                "measurement_ids": [
                    measurement.measurement_id for measurement in item.measurements
                ],
            }
            for alias, item in resolved.items()
        }
        evaluation = EvaluationRecord(
            evaluation_id=f"evaluation-{stable_id}",
            operation_id=binding.operation_id,
            metric_id=binding.metric_id,
            measurement_ids=measurement_ids,
            rule_id=binding.rule_id,
            rule_version=rule_version,
            rule_hash=rule_hash,
            operator=binding.operator,
            expected=parameter.value,
            actual=actual.value,
            outcome="pass" if passed else "fail",
            feature_refs=sorted(
                set(binding.feature_refs)
                | {
                    ref
                    for operand in binding.additional_operands
                    for ref in operand.feature_refs
                }
                | {ref for item in linked for ref in item.feature_refs}
            ),
            region_refs=sorted(
                set(binding.region_refs)
                | {
                    ref
                    for operand in binding.additional_operands
                    for ref in operand.region_refs
                }
                | {ref for item in linked for ref in item.region_refs}
            ),
            check_id=binding.check_id,
            actual_unit=actual.unit,
            expression=binding.expression,
            operand_values=operand_values,
        )
        source = {
            "type": "effective_rule",
            "binding_id": binding.binding_id,
            "check_id": binding.check_id,
            "source": parameter.source,
            "version": parameter.version,
            "unit": parameter.unit,
            "expression": expression,
            "operands": operand_values,
        }
        return evaluation, source

    def _resolve_operand(
        self,
        measurements: list[MeasurementRecord],
        plan: PlanRecord,
        binding: RuleBinding,
        operand: RuleOperand,
    ) -> _ResolvedOperand:
        allow_unscoped_whole_model = self._allows_unscoped_whole_model(
            plan, operand
        )
        matches = sorted(
            (
                measurement
                for measurement in measurements
                if measurement.status == "measured"
                and measurement.operation_id == operand.operation_id
                and measurement.metric_id == operand.metric_id
                and measurement.quantity_id == operand.quantity_id
                and (
                    (
                        set(operand.feature_refs).issubset(measurement.feature_refs)
                        and set(operand.region_refs).issubset(measurement.region_refs)
                    )
                    or (
                        allow_unscoped_whole_model
                        and not measurement.feature_refs
                        and not measurement.region_refs
                    )
                )
            ),
            key=lambda item: item.measurement_id,
        )
        if not matches:
            raise DFMError(
                "evaluation_operand_missing",
                "A required Measurement operand is absent from the objective result.",
                {
                    "binding_id": binding.binding_id,
                    "operand_alias": operand.alias,
                    "operation_id": operand.operation_id,
                    "metric_id": operand.metric_id,
                    "quantity_id": operand.quantity_id,
                },
            )
        units = {item.unit for item in matches}
        if len(units) != 1:
            raise DFMError(
                "evaluation_unit_invalid",
                "Measurements aggregated into one operand must use one unit.",
                {"binding_id": binding.binding_id, "operand_alias": operand.alias},
            )
        values = [item.value for item in matches]
        if operand.aggregation == "identity":
            if len(values) != 1:
                raise DFMError(
                    "evaluation_operand_ambiguous",
                    "Identity aggregation requires exactly one matching Measurement.",
                    {
                        "binding_id": binding.binding_id,
                        "operand_alias": operand.alias,
                        "measurement_ids": [item.measurement_id for item in matches],
                    },
                )
            value = values[0]
        elif operand.aggregation == "count":
            value = len(values)
            units = {None}
        else:
            numbers = [
                self._finite_number(
                    value,
                    binding_id=binding.binding_id,
                    operand_alias=operand.alias,
                )
                for value in values
            ]
            reducers = {
                "minimum": min,
                "maximum": max,
                "mean": statistics.fmean,
                "median": statistics.median,
                "sum": sum,
            }
            value = reducers[operand.aggregation](numbers)
        return _ResolvedOperand(
            alias=operand.alias,
            value=value,
            unit=next(iter(units)),
            aggregation=operand.aggregation,
            measurements=tuple(matches),
        )

    @staticmethod
    def _allows_unscoped_whole_model(
        plan: PlanRecord, operand: RuleOperand
    ) -> bool:
        """Allow the OCCT v2 bridge only for the explicit whole-model region."""

        if not operand.region_refs:
            return False
        regions = {region.region_id: region for region in plan.regions}
        selected = [regions.get(region_id) for region_id in operand.region_refs]
        if any(region is None or region.mode != "whole_model" for region in selected):
            return False
        selected_feature_refs = {
            feature_ref
            for region in selected
            if region is not None
            for feature_ref in region.feature_refs
        }
        return set(operand.feature_refs).issubset(selected_feature_refs)

    def _evaluate_expression(
        self,
        expression: dict[str, Any],
        operands: dict[str, _ResolvedOperand],
        binding_id: str,
    ) -> _ExpressionValue:
        if "operand" in expression:
            alias = str(expression["operand"])
            operand = operands.get(alias)
            if operand is None:
                raise DFMError(
                    "evaluation_expression_invalid",
                    "A rule expression references an unresolved operand.",
                    {"binding_id": binding_id, "operand_alias": alias},
                )
            return _ExpressionValue(operand.value, operand.unit)
        if "constant" in expression:
            return _ExpressionValue(expression["constant"], expression.get("unit"))

        operation = str(expression.get("op") or "")
        arguments = [
            self._evaluate_expression(item, operands, binding_id)
            for item in expression.get("args", [])
        ]
        if operation in {"abs", "negate"}:
            number = self._finite_number(
                arguments[0].value, binding_id=binding_id, operand_alias=operation
            )
            return _ExpressionValue(
                abs(number) if operation == "abs" else -number,
                arguments[0].unit,
            )
        numbers = [
            self._finite_number(
                item.value, binding_id=binding_id, operand_alias=operation
            )
            for item in arguments
        ]
        if operation in {"add", "subtract", "minimum", "maximum"}:
            unit = self._same_expression_unit(arguments, binding_id, operation)
            if operation == "add":
                value = sum(numbers)
            elif operation == "subtract":
                value = numbers[0] - numbers[1]
            elif operation == "minimum":
                value = min(numbers)
            else:
                value = max(numbers)
            return _ExpressionValue(value, unit)
        if operation == "multiply":
            value = math.prod(numbers)
            dimensionful = [
                item.unit for item in arguments if item.unit not in _DIMENSIONLESS_UNITS
            ]
            if len(dimensionful) > 1:
                raise DFMError(
                    "evaluation_unit_invalid",
                    "Multiplication of two dimensionful rule operands is unsupported.",
                    {"binding_id": binding_id},
                )
            return _ExpressionValue(value, dimensionful[0] if dimensionful else None)
        if operation == "divide":
            if numbers[1] == 0:
                raise DFMError(
                    "evaluation_expression_invalid",
                    "A rule expression attempted division by zero.",
                    {"binding_id": binding_id},
                )
            left, right = arguments
            if left.unit == right.unit and left.unit not in {None, ""}:
                unit = "ratio"
            elif right.unit in _DIMENSIONLESS_UNITS:
                unit = left.unit
            elif left.unit in _DIMENSIONLESS_UNITS:
                raise DFMError(
                    "evaluation_unit_invalid",
                    "Division by a dimensionful operand has no supported result unit.",
                    {"binding_id": binding_id},
                )
            else:
                raise DFMError(
                    "evaluation_unit_invalid",
                    "Rule expression division requires compatible operand units.",
                    {"binding_id": binding_id, "units": [left.unit, right.unit]},
                )
            return _ExpressionValue(numbers[0] / numbers[1], unit)
        raise DFMError(
            "evaluation_expression_invalid",
            "A rule expression operation is unsupported.",
            {"binding_id": binding_id, "operation": operation},
        )

    @staticmethod
    def _same_expression_unit(
        values: list[_ExpressionValue], binding_id: str, operation: str
    ) -> str | None:
        units = {item.unit for item in values}
        if len(units) != 1:
            raise DFMError(
                "evaluation_unit_invalid",
                "Rule expression operands use incompatible units.",
                {
                    "binding_id": binding_id,
                    "operation": operation,
                    "units": sorted(str(item) for item in units),
                },
            )
        return next(iter(units))

    @staticmethod
    def _finite_number(value: Any, *, binding_id: str, operand_alias: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DFMError(
                "evaluation_value_invalid",
                "Rule expression arithmetic requires numeric scalar operands.",
                {"binding_id": binding_id, "operand_alias": operand_alias},
            )
        number = float(value)
        if not math.isfinite(number):
            raise DFMError(
                "evaluation_value_invalid",
                "Rule expression operands must be finite.",
                {"binding_id": binding_id, "operand_alias": operand_alias},
            )
        return number

    @staticmethod
    def _validate_result_unit(
        actual_unit: str | None,
        expected_unit: str | None,
        *,
        binding_id: str,
    ) -> None:
        if expected_unit is not None and actual_unit != expected_unit:
            raise DFMError(
                "evaluation_unit_invalid",
                "The rule threshold unit does not match the expression result unit.",
                {
                    "binding_id": binding_id,
                    "actual_unit": actual_unit,
                    "expected_unit": expected_unit,
                },
            )

    @staticmethod
    def _compare(
        operation_name: str,
        actual: Any,
        expected: Any,
        *,
        binding_id: str,
    ) -> bool:
        if operation_name == "between":
            if isinstance(expected, dict):
                lower, upper = expected.get("lower"), expected.get("upper")
            elif isinstance(expected, (list, tuple)) and len(expected) == 2:
                lower, upper = expected
            else:
                raise DFMError(
                    "evaluation_rule_invalid",
                    "Between rules require lower and upper threshold values.",
                    {"binding_id": binding_id},
                )
            values = (lower, actual, upper)
            if (
                any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in values
                )
                or lower > upper
            ):
                raise DFMError(
                    "evaluation_rule_invalid",
                    "Between rules require a finite ordered numeric range.",
                    {"binding_id": binding_id},
                )
            try:
                return bool(lower <= actual <= upper)
            except TypeError as exc:
                raise DFMError(
                    "evaluation_value_invalid",
                    "Expression result and threshold range cannot be compared.",
                    {"binding_id": binding_id},
                ) from exc
        comparison = _OPERATORS.get(operation_name)
        if comparison is None:
            raise DFMError(
                "evaluation_rule_invalid",
                "Evaluation operator is not supported.",
                {"binding_id": binding_id, "operator": operation_name},
            )
        try:
            return bool(comparison(actual, expected))
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "evaluation_value_invalid",
                "Expression result and expected value cannot be compared.",
                {"binding_id": binding_id},
            ) from exc

    @staticmethod
    def _rule_hash(
        *,
        binding: RuleBinding,
        rule_version: str,
        expected: Any,
        expected_unit: str | None,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "binding": binding.to_dict(),
                    "rule_version": rule_version,
                    "expected": expected,
                    "unit": expected_unit,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _evaluate_legacy_measurement(
        self,
        measurement: MeasurementRecord,
        plan: PlanRecord,
        spec: dict[str, Any],
    ) -> tuple[EvaluationRecord, dict[str, Any]]:
        rule_id = str(spec["rule_id"])
        operation_name = str(spec["operator"])
        expected = spec.get("fallback_expected")
        if expected is None:
            raise DFMError(
                "evaluation_rule_missing",
                "No effective parameter exists for a measured check.",
                {"rule_id": rule_id, "measurement_id": measurement.measurement_id},
            )
        passed = self._compare(
            operation_name,
            measurement.value,
            expected,
            binding_id=f"legacy:{measurement.measurement_id}",
        )
        rule_version = plan.scope_version or "1"
        rule_hash = hashlib.sha256(
            json.dumps(
                {
                    "rule_id": rule_id,
                    "rule_version": rule_version,
                    "operator": operation_name,
                    "expected": expected,
                    "unit": measurement.unit,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        legacy_id = str(
            measurement.diagnostics.get("legacy_issue_id") or measurement.measurement_id
        )
        return (
            EvaluationRecord(
                evaluation_id=f"evaluation-{legacy_id.lower()}",
                operation_id=measurement.operation_id,
                metric_id=measurement.metric_id,
                measurement_ids=[measurement.measurement_id],
                rule_id=rule_id,
                rule_version=rule_version,
                rule_hash=rule_hash,
                operator=operation_name,
                expected=expected,
                actual=measurement.value,
                outcome="pass" if passed else "fail",
                feature_refs=sorted(measurement.feature_refs),
                region_refs=sorted(measurement.region_refs),
                actual_unit=measurement.unit,
            ),
            {"type": "measurement_rule_snapshot", "source": "step_adapter"},
        )

    @staticmethod
    def _legacy_spec(
        measurement: MeasurementRecord, plan: PlanRecord
    ) -> dict[str, Any] | None:
        if (
            plan.process == "die_casting"
            and measurement.operation_id == "geometry.topology"
            and measurement.quantity_id == "valid_brep"
        ):
            return {
                "rule_id": "valid_brep_required",
                "operator": "==",
                "fallback_expected": True,
            }
        hint = measurement.diagnostics.get("evaluation_hint")
        return dict(hint) if isinstance(hint, dict) else None
