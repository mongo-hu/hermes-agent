"""Validate the CLI protocol and the capabilities needed by a concrete plan."""

from __future__ import annotations

import math
import re
from typing import Any, Sequence

from ..contracts import OBJECTIVE_SCHEMA_VERSION, PlanOperation
from ..errors import DFMError


def _strings(value: object) -> bool:
    return (isinstance(value, list)
            and all(isinstance(item, str) and item for item in value)
            and len(value) == len(set(value)))


def _finite_number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid capability field")


def validate_capabilities(payload: dict[str, Any]) -> None:
    """Validate structure independently of the caller's enabled operations."""
    try:
        _require(payload["contract_version"] == "dfm.geometry.capabilities/v1")
        _require(type(payload["objective_schema_version"]) is int)
        _require(payload["objective_schema_version"] == OBJECTIVE_SCHEMA_VERSION)
        _require(payload["backend"] == "analysis_situs+occt")
        _require(payload["status"] == "available")
        _require(payload["maturity"] == "experimental")
        for key in ("engine_version", "analysis_situs_version", "analysis_situs_commit", "occt_version"):
            _require(isinstance(payload[key], str) and payload[key])
        _require(re.fullmatch(r"occt-dfm-geometry-[0-9]+\.[0-9]+\.[0-9]+", payload["engine_version"]) is not None)
        for key in ("supported_processes", "supported_formats", "supported_extensions", "output_artifact_kinds"):
            _require(_strings(payload[key]) and payload[key])
        limits = payload["runtime_limits"]
        for key in ("recommended_process_timeout_seconds", "maximum_operation_timeout_seconds", "progress_heartbeat_interval_seconds"):
            _require(type(limits[key]) is int and 0 < limits[key] <= 86400)
        _require(limits["recommended_process_timeout_seconds"] >= limits["maximum_operation_timeout_seconds"])
        operations = payload["operations"]
        _require(isinstance(operations, list) and operations)
        identities, calculators = set(), set()
        for item in operations:
            for key in ("operation_id", "calculator_id", "process", "algorithm_version"):
                _require(isinstance(item[key], str) and item[key])
            _require(item["operation_id"] not in identities)
            _require(item["calculator_id"] not in calculators)
            identities.add(item["operation_id"])
            calculators.add(item["calculator_id"])
            _require(item["kind"] in {"infrastructure", "feature_recognition", "measurement"})
            _require(item["status"] in {"available", "disabled", "unavailable"})
            _require(item["maturity"] == "experimental")
            _require(isinstance(item["limits"], dict))
            for key in ("depends_on", "metric_ids", "required_quantities", "required_artifacts"):
                _require(_strings(item[key]))
            names = set()
            _require(isinstance(item["algorithm_options"], list))
            for option in item["algorithm_options"]:
                _require(isinstance(option["name"], str) and option["name"])
                _require(option["name"] not in names)
                names.add(option["name"])
                _require(option["type"] in {"number", "integer", "boolean"})
                for bound in ("minimum", "exclusive_minimum", "maximum"):
                    if bound in option:
                        _require(_finite_number(option[bound]))
        _require(all(set(item["depends_on"]) <= identities for item in operations))
    except (AssertionError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DFMError("geometry_protocol_invalid", "The DFM geometry capability contract is incompatible.") from exc


def validate_operations(payload: dict[str, Any], operations: Sequence[PlanOperation],
                        *, process: str = "injection") -> None:
    catalog = {item["operation_id"]: item for item in payload["operations"]}
    state: dict[str, int] = {}

    def require(identity: str) -> dict[str, Any]:
        item = catalog.get(identity)
        if item is None or item["status"] != "available" or item["process"] != process:
            raise DFMError("unsupported_calculator", "A required geometry operation is unavailable.",
                           {"operation_id": identity, "reason": item.get("unavailable_reason") if item else "missing"})
        if item["algorithm_version"] != payload["engine_version"]:
            raise DFMError("geometry_protocol_invalid", "This adapter requires engine-bundle algorithm versions.",
                           {"operation_id": identity})
        if state.get(identity) == 1:
            raise DFMError("geometry_protocol_invalid", "Geometry capability dependencies contain a cycle.")
        if state.get(identity) != 2:
            state[identity] = 1
            for dependency in item["depends_on"]:
                require(dependency)
            state[identity] = 2
        return item

    for operation in operations:
        matches = [identity for identity, item in catalog.items()
                   if item["calculator_id"] == operation.calculator_id
                   and (operation.operation_id == identity
                        or (operation.operation_id.startswith(identity + ".")
                            and len(operation.operation_id) > len(identity) + 1))]
        if len(matches) != 1:
            raise DFMError("unsupported_calculator", "The planned operation is not supported by this executable.",
                           {"operation_id": operation.operation_id})
        item = require(matches[0])
        for key in ("depends_on", "metric_ids", "required_quantities", "required_artifacts"):
            if list(getattr(operation, key)) != item[key]:
                raise DFMError("geometry_protocol_invalid", "The plan does not match its geometry operation contract.",
                               {"operation_id": operation.operation_id, "field": key})
        specs = {option["name"]: option for option in item["algorithm_options"]}
        for name, argument in operation.algorithm_options.items():
            spec = specs.get(name)
            value = argument.value
            valid = spec is not None
            if valid:
                expected = spec["type"]
                valid = (type(value) is bool if expected == "boolean" else
                         type(value) is int if expected == "integer" else
                         type(value) in (int, float))
            if valid and type(value) in (int, float):
                valid = (_finite_number(value)
                         and ("minimum" not in spec or value >= spec["minimum"])
                         and ("exclusive_minimum" not in spec or value > spec["exclusive_minimum"])
                         and ("maximum" not in spec or value <= spec["maximum"]))
            if not valid:
                raise DFMError("objective_task_invalid", f"Algorithm option {name} for {operation.operation_id} is unsupported or outside its limits.",
                               {"operation_id": operation.operation_id, "option": name})
