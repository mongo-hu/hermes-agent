"""Confirmation identities derived from engineering content, not transport state."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

from .contracts import FeatureRecord, FormingScopeRecord, ReviewGroupRecord


def _canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def feature_review_content(feature: FeatureRecord) -> dict:
    properties = feature.properties
    return {
        "feature_id": feature.feature_id,
        "kind": feature.kind,
        "input_sha256": feature.input_sha256,
        "subtype": properties.get("subtype"),
        "geometry_refs": sorted(properties.get("geometry_refs", []), key=_canonical),
        "parts": {
            name: sorted(refs, key=_canonical)
            for name, refs in properties.get("parts", {}).items()
        },
        "parameters": properties.get("parameters", {}),
        "relationships": sorted(
            (
                {
                    key: relation[key]
                    for key in (
                        "relation_id",
                        "type",
                        "from_ref",
                        "to_ref",
                        "geometry_refs",
                        "patch_mask_ref",
                    )
                    if key in relation
                }
                for relation in feature.relationships
                if relation.get("type")
                not in {"intersects_forming_scope", "contained_in_forming_scope"}
            ),
            key=_canonical,
        ),
    }


def local_structure_review_content(
    scope: FormingScopeRecord, features: list[FeatureRecord], relationships: list[dict]
) -> dict:
    """Scope membership is structural; a new direction/handle is not."""
    relations = [
        {
            key: row[key]
            for key in (
                "relation_id",
                "type",
                "from_ref",
                "to_ref",
                "geometry_refs",
                "patch_mask_ref",
            )
            if key in row
        }
        for row in relationships
        if row.get("to_ref") == scope.forming_scope_id
        and row.get("type")
        in {"intersects_forming_scope", "contained_in_forming_scope"}
    ]
    members = {row["from_ref"] for row in relations}
    return {
        "forming_scope": {
            "forming_scope_id": scope.forming_scope_id,
            "kind": scope.kind,
            "mode": scope.mode,
            "geometry_refs": sorted(
                (ref.to_dict() for ref in scope.geometry_refs), key=_canonical
            ),
        },
        "features": [
            feature_review_content(feature)
            for feature in sorted(features, key=lambda f: f.feature_id)
            if feature.feature_id in members
        ],
        "relationships": sorted(relations, key=_canonical),
    }


def structure_review_groups(
    features: list[FeatureRecord],
    existing: list[ReviewGroupRecord],
    *,
    snapshot_id: str,
    created_at: str,
) -> list[ReviewGroupRecord]:
    expected = {}
    for feature in sorted(features, key=lambda value: value.feature_id):
        digest = hashlib.sha256(
            _canonical(feature_review_content(feature)).encode("utf-8")
        ).hexdigest()
        group = ReviewGroupRecord(
            review_group_id=f"review.structure.{digest[:24]}",
            kind="ordinary_structure",
            target_refs=[feature.feature_id],
            content_sha256=digest,
            source="user",
            status="pending_review",
            created_at=created_at,
            snapshot_id=snapshot_id,
        )
        expected[group.review_group_id] = group
    return _reconcile_groups(
        existing, expected, lambda group: group.kind == "ordinary_structure"
    )


def annotation_review_groups(
    annotations: list[dict],
    existing: list[ReviewGroupRecord],
    *,
    snapshot_id: str,
    created_at: str,
) -> list[ReviewGroupRecord]:
    """Each adopted surface property has an independent engineering approval."""
    expected = {}
    for annotation in annotations:
        if annotation.get("status", "active") == "stale":
            continue
        content = {
            "kind": annotation["kind"],
            "value": annotation.get("value"),
            "geometry_refs": sorted(
                annotation.get("geometry_refs", []), key=_canonical
            ),
            "properties": annotation.get("properties", {}),
        }
        digest = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
        identity = f"review.attribute.{annotation['annotation_id']}.{digest[:16]}"
        expected[identity] = ReviewGroupRecord(
            review_group_id=identity,
            kind="surface_attribute",
            target_refs=[annotation["annotation_id"]],
            content_sha256=digest,
            source="user",
            status="pending_review",
            created_at=created_at,
            snapshot_id=snapshot_id,
        )
    return _reconcile_groups(
        existing, expected, lambda group: group.kind == "surface_attribute"
    )


def _reconcile_groups(existing, expected, applies):
    result = []
    for group in existing:
        if not applies(group):
            result.append(group)
        elif group.review_group_id in expected:
            # Returning to a previously reviewed geometry still requires review;
            # never leave the only matching group stale or revive its old approval.
            result.append(
                expected[group.review_group_id] if group.status == "stale" else group
            )
        else:
            result.append(
                replace(group, status="stale")
                if group.status in {"pending_review", "confirmed"}
                else group
            )
    known = {group.review_group_id for group in result}
    result.extend(
        group for identity, group in expected.items() if identity not in known
    )
    return result


def local_structure_review_groups(
    scopes: list[FormingScopeRecord],
    features: list[FeatureRecord],
    relationships: list[dict],
    existing: list[ReviewGroupRecord],
    *,
    scope_ids: set[str],
    snapshot_id: str,
    created_at: str,
) -> list[ReviewGroupRecord]:
    """Refresh affected local structure reviews, without touching directions."""
    expected = {}
    for scope in scopes:
        if (
            scope.forming_scope_id not in scope_ids
            or scope.mode != "local"
            or scope.status == "stale"
        ):
            continue
        content = local_structure_review_content(scope, features, relationships)
        digest = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
        members = sorted({row["from_ref"] for row in content["relationships"]})
        group = ReviewGroupRecord(
            review_group_id=f"review.local-structure.{scope.forming_scope_id}.{digest[:16]}",
            kind="local_structure",
            target_refs=[scope.forming_scope_id, *members],
            content_sha256=digest,
            source="user",
            status="pending_review",
            created_at=created_at,
            snapshot_id=snapshot_id,
        )
        expected[group.review_group_id] = group
    return _reconcile_groups(
        existing,
        expected,
        lambda group: (
            group.kind == "local_structure"
            and bool(scope_ids.intersection(group.target_refs))
        ),
    )
