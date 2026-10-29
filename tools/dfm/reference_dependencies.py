"""Resolve a G's declared reference objects from one immutable geometry graph."""

from __future__ import annotations

from typing import Mapping, Sequence

from .contracts import FeatureRecord, ReviewGroupRecord


def resolve_reference_dependencies(
    feature: FeatureRecord,
    geometric_id: str,
    requirements: list[dict],
    *,
    features: Mapping[str, FeatureRecord],
    relationships: Mapping[tuple[str, str], Sequence[dict]],
    reviews: Mapping[str, Sequence[ReviewGroupRecord]],
) -> tuple[list[dict], list[ReviewGroupRecord], list[dict]]:
    """Return resolved inputs, required reviews and explicit blocking reasons.

    Indices are derived once per Plan. Reference objects do not have to belong
    to the source Feature's local forming scope.
    """
    dependencies, groups, blockers = [], [], []
    for requirement in requirements:
        relation_type = requirement["type"]
        candidates = list(relationships.get((feature.feature_id, relation_type), ()))
        reason = {
            "feature_id": feature.feature_id,
            "geometric_id": geometric_id,
            "relationship_type": relation_type,
            "target_feature_kind": requirement["target_feature_kind"],
        }
        if len(candidates) != 1:
            blockers.append({
                **reason,
                "code": "missing_reference_relationship"
                if not candidates
                else "ambiguous_reference_relationship",
                "candidate_feature_refs": sorted({
                    str(row.get("to_ref", "")) for row in candidates
                }),
            })
            continue
        relation = candidates[0]
        target = features.get(str(relation.get("to_ref") or ""))
        refs = relation.get("geometry_refs")
        if (
            target is None
            or target.kind != requirement["target_feature_kind"]
            or target.input_sha256 != feature.input_sha256
            or not isinstance(refs, list)
            or not refs
            or relation.get("patch_mask_ref") is not None
            or any(
                not isinstance(ref, dict)
                or ref.get("kind") != "face"
                or ref not in target.properties.get("geometry_refs", [])
                for ref in refs
            )
        ):
            blockers.append({
                **reason,
                "code": "invalid_reference_relationship",
                "relation_id": relation.get("relation_id"),
                "target_feature_id": relation.get("to_ref"),
            })
            continue
        required = list(reviews.get(target.feature_id, ()))
        groups.extend(required)
        dependencies.append({
            "feature_id": feature.feature_id,
            "geometric_id": geometric_id,
            "relation_id": relation["relation_id"],
            "type": relation_type,
            "target_feature_id": target.feature_id,
            "target_feature_kind": target.kind,
            "geometry_refs": refs,
            "review_group_refs": sorted(group.review_group_id for group in required),
        })
        if not required or any(group.status != "confirmed" for group in required):
            blockers.append({
                **reason,
                "code": "pending_review",
                "target_feature_id": target.feature_id,
                "review_group_refs": sorted(
                    group.review_group_id
                    for group in required
                    if group.status != "confirmed"
                ),
            })
    return dependencies, groups, blockers
