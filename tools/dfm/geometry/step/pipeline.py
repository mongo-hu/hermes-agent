"""Versioned operation validation and check-family selection for STEP M1.2."""

from __future__ import annotations

from ...contracts import PlanOperation
from ...errors import DFMError
from ..brep.checks import BREP_CHECK_FAMILIES, resolve_brep_check


OPERATION_CHECK_FAMILIES = BREP_CHECK_FAMILIES


def validate_operations(operations: list[PlanOperation]) -> list[str]:
    if not operations:
        raise DFMError(
            "worker_request_invalid",
            "The STEP worker requires persisted plan operations.",
        )
    ids: set[str] = set()
    ordered: list[str] = []
    for item in operations:
        if item.operation_id in ids:
            raise DFMError(
                "worker_request_invalid",
                "DFM plan operation ids must be unique.",
                {"operation_id": item.operation_id},
            )
        resolve_brep_check(item.calculator_id)
        missing = [
            dependency for dependency in item.depends_on if dependency not in ids
        ]
        if missing:
            raise DFMError(
                "worker_request_invalid",
                "DFM plan operations must be dependency ordered.",
                {"operation_id": item.operation_id, "missing_dependencies": missing},
            )
        ids.add(item.operation_id)
        ordered.append(item.calculator_id)
    if ordered[0] != "load_geometry" or "inspect_topology" not in ordered:
        raise DFMError(
            "worker_request_invalid",
            "STEP plans must load the model and inspect topology first.",
        )
    return ordered
