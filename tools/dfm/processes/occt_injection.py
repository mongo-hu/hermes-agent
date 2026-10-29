"""Compile injection rules directly against the public dfm.geometry/v2 contract."""

from __future__ import annotations

from typing import Any, Mapping

from ..analyzers.base import AnalyzerContext
from ..errors import DFMError
from ..runtime.process import SESSION_CONTRACT
from .base import ProcessPlan
from .injection import InjectionProcessAdapter


def geometry_binding_index(scope: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate public Geometric capabilities without exposing Worker internals."""

    if scope.get("contract_version") != SESSION_CONTRACT:
        raise DFMError(
            "process_scope_invalid",
            "The OCCT geometry scope must use dfm.geometry/v2.",
        )
    raw_geometrics = scope.get("geometrics")
    if not isinstance(raw_geometrics, list) or not raw_geometrics:
        raise DFMError(
            "process_scope_invalid",
            "The OCCT geometry scope must publish Geometric-ID capabilities.",
        )
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_geometrics:
        if not isinstance(raw, Mapping):
            raise DFMError(
                "process_scope_invalid",
                "An OCCT Geometric capability is not an object.",
            )
        geometric_id = str(raw.get("geometric_id") or "")
        if not geometric_id.startswith("G_") or geometric_id in result:
            raise DFMError(
                "process_scope_invalid",
                "OCCT Geometric capabilities must have unique published G IDs.",
                {"geometric_id": geometric_id},
            )
        if raw.get("support_status") != "supported":
            continue
        feature_kind = str(raw.get("feature_kind") or "")
        scope_modes = raw.get("scope_modes")
        direction_modes = raw.get("direction_modes")
        context_schema = raw.get("context_schema")
        reference_requirements = raw.get("requires_relationships")
        if (
            not feature_kind
            or not isinstance(scope_modes, list)
            or not scope_modes
            or not isinstance(direction_modes, list)
            or not direction_modes
            or not isinstance(raw.get("requires_complete_inventory"), list)
            or any(not isinstance(kind, str) or not kind for kind in raw.get("requires_complete_inventory", []))
            or not isinstance(reference_requirements, list)
            or any(not isinstance(item, Mapping) or set(item) != {"type", "target_feature_kind"}
                   or any(not isinstance(item.get(key), str) or not item[key] for key in ("type", "target_feature_kind"))
                   for item in reference_requirements)
            or len({item["type"] for item in reference_requirements}) != len(reference_requirements)
            or not isinstance(context_schema, Mapping)
        ):
            raise DFMError(
                "process_scope_invalid",
                "A supported Geometric ID has an invalid public declaration.",
                {"geometric_id": geometric_id},
            )
        result[geometric_id] = {
            "feature_kind": feature_kind,
            "scope_modes": [str(value) for value in scope_modes],
            "direction_modes": [str(value) for value in direction_modes],
            "requires_complete_inventory": list(raw["requires_complete_inventory"]),
            "requires_relationships": [dict(item) for item in reference_requirements],
            "context_schema": dict(context_schema),
        }
    return result


def compile_occt_injection_plan(
    adapter: InjectionProcessAdapter,
    context: AnalyzerContext,
    raw_parameters: Mapping[str, Any],
    capability: Mapping[str, Any] | None,
    *,
    scoped_facts: Mapping[str, Mapping[str, Any]] | None = None,
) -> ProcessPlan:
    """Compile Check operands to Feature + G + context, with no operation graph."""

    if capability is None:
        raise DFMError(
            "geometry_dependency_missing",
            "The configured OCCT Worker is unavailable; v2 planning cannot continue.",
        )
    supported = geometry_binding_index(capability)
    adapter.ontology_store.configure_geometric_bindings(supported)
    base = adapter.compile(
        context,
        raw_parameters,
        **({"scoped_facts": scoped_facts} if scoped_facts else {}),
    )
    bindings = list(base.rule_bindings)
    binding_ids = {item.binding_id for item in bindings}
    rule_ids = {item.rule_id for item in bindings}
    return ProcessPlan(
        process=base.process,
        adapter_version=f"{base.adapter_version}+dfm.geometry-v2",
        scope_id=base.scope_id,
        scope_version=base.scope_version,
        rules={key: value for key, value in base.rules.items() if key in rule_ids},
        operations=[],
        accepted_inputs=base.accepted_inputs,
        rule_bindings=bindings,
        binding_selectors={
            key: value
            for key, value in base.binding_selectors.items()
            if key in binding_ids
        },
        ontology_snapshot_id=base.ontology_snapshot_id,
        ontology_snapshot_sha256=base.ontology_snapshot_sha256,
        skipped_checks=base.skipped_checks,
    )
