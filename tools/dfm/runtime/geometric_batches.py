"""Bounded transport batches; dependency fragments keep their own identities."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Collection

from ..contracts import PlanFragmentRecord, PlanRecord


MAX_BATCH_REQUESTS = 8


def request_key(
    feature_id: str, geometric_id: str, context: dict
) -> tuple[str, str, str]:
    return (
        feature_id,
        geometric_id,
        json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False),
    )


def _keys(fragment: PlanFragmentRecord) -> set[tuple[str, str, str]]:
    return {
        request_key(item.feature_id, geometric_id, item.context)
        for item in fragment.items
        for geometric_id in item.geometric_ids
    }


@dataclass(frozen=True)
class GeometricBatch:
    snapshot_id: str
    fragments: tuple[PlanFragmentRecord, ...]

    @property
    def fragment_refs(self) -> frozenset[str]:
        return frozenset(fragment.fragment_id for fragment in self.fragments)

    def owners(self) -> dict[tuple[str, str, str], list[PlanFragmentRecord]]:
        result: dict[tuple[str, str, str], list[PlanFragmentRecord]] = {}
        for fragment in self.fragments:
            for key in sorted(_keys(fragment)):
                result.setdefault(key, []).append(fragment)
        return result

    def items(self) -> list[dict]:
        groups: dict[tuple[str, str], dict] = {}
        for fragment in self.fragments:
            for item in fragment.items:
                _, _, context = request_key(item.feature_id, "", item.context)
                group = groups.setdefault(
                    (item.feature_id, context),
                    {
                        "feature_id": item.feature_id,
                        "geometric_ids": [],
                        "context": dict(item.context),
                    },
                )
                for geometric_id in item.geometric_ids:
                    if geometric_id not in group["geometric_ids"]:
                        group["geometric_ids"].append(geometric_id)
        return list(groups.values())


def next_geometric_batch(
    plan: PlanRecord,
    completed: Collection[str],
    *,
    selected: Collection[str] | None = None,
    max_requests: int = MAX_BATCH_REQUESTS,
) -> GeometricBatch | None:
    """Favor a checkable first conclusion; later batches amortize IPC overhead.

    The budget counts distinct Feature/G/context requests, not consumer bindings.
    Oversized individual fragments run alone rather than being partially marked done.
    """
    if max_requests < 1:
        raise ValueError("A Geometric batch requires a positive request budget")
    active = plan.active_plan_fragments()
    ready = [
        fragment
        for fragment in active
        if fragment.status == "ready"
        and fragment.fragment_id not in completed
        and (selected is None or fragment.fragment_id in selected)
    ]
    if not ready:
        return None
    blocked_bindings = {
        identity
        for fragment in active
        if fragment.status in {"pending_review", "waiting_input", "needs_attention"}
        for identity in fragment.rule_binding_refs
    }
    seed = next(
        (
            fragment
            for fragment in ready
            if any(
                identity not in blocked_bindings
                for identity in fragment.rule_binding_refs
            )
        ),
        ready[0],
    )
    bindings = [
        identity
        for identity in seed.rule_binding_refs
        if identity not in blocked_bindings
    ]

    def cohort_size(identity):
        return len(
            set().union(
                *(
                    _keys(fragment)
                    for fragment in ready
                    if identity in fragment.rule_binding_refs
                )
            )
        )

    bounded = [
        identity for identity in bindings if cohort_size(identity) <= max_requests
    ]
    binding = (
        (max(bounded, key=cohort_size) if bounded else min(bindings, key=cohort_size))
        if bindings
        else None
    )
    features = {item.feature_id for item in seed.items}
    candidates = [
        fragment
        for fragment in ready
        if fragment.snapshot_id == seed.snapshot_id
        and fragment.capability_version == seed.capability_version
    ]
    candidates.sort(
        key=lambda fragment: (
            fragment.fragment_id != seed.fragment_id,
            not bool(
                set(fragment.rule_binding_refs).intersection(seed.rule_binding_refs)
            ),
            not any(item.feature_id in features for item in fragment.items),
        )
    )
    first_response = not completed
    chosen, keys = [], set()
    for fragment in candidates:
        added = _keys(fragment)
        if first_response and fragment != seed and not added.issubset(keys):
            if binding is not None and binding not in fragment.rule_binding_refs:
                continue
            if binding is None and not any(
                item.feature_id in features for item in fragment.items
            ):
                continue
        if chosen and len(keys | added) > max_requests:
            continue
        chosen.append(fragment)
        keys.update(added)
    return GeometricBatch(seed.snapshot_id, tuple(chosen))
