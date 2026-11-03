"""Project review decisions layered over immutable Worker feature snapshots."""

from __future__ import annotations

from dataclasses import replace
import hashlib

from .contracts import FeatureRecord, PlanFragmentRecord, ProjectManifest


def excluded_feature_ids(manifest: ProjectManifest, input_sha256: str) -> set[str]:
    return set(manifest.capabilities.get("feature_exclusions", {}).get(input_sha256, {}))


def apply_feature_exclusions(features: list[FeatureRecord], capabilities: dict) -> list[FeatureRecord]:
    exclusions = capabilities.get("feature_exclusions", {})
    return [
        replace(feature, status="rejected")
        if feature.feature_id in exclusions.get(feature.input_sha256, {})
        else feature
        for feature in features
    ]


def active_features(manifest: ProjectManifest, feature_refs=None) -> list[FeatureRecord]:
    refs = set(feature_refs) if feature_refs is not None else None
    return [feature for feature in apply_feature_exclusions(manifest.features, manifest.capabilities)
            if feature.status != "rejected" and (refs is None or feature.feature_id in refs)]


def without_excluded_relationships(relationships: list[dict], excluded_ids: set[str]) -> list[dict]:
    return [row for row in relationships
            if row.get("from_ref") not in excluded_ids and row.get("to_ref") not in excluded_ids]


def fragment_uses_excluded_feature(fragment: PlanFragmentRecord, excluded_ids: set[str]) -> bool:
    return any(item.feature_id in excluded_ids for item in fragment.items) or any(
        row["target_feature_id"] in excluded_ids for row in fragment.reference_dependencies)


def retire_excluded_fragments(
    parent_fragments: list[PlanFragmentRecord], replacements: list[PlanFragmentRecord],
    excluded_ids: set[str], snapshot_id: str,
) -> list[PlanFragmentRecord]:
    """Supersede tasks that disappeared, without rewriting immutable history."""
    superseded = {row.supersedes for row in replacements if row.supersedes}
    retired = []
    for fragment in parent_fragments:
        if not fragment_uses_excluded_feature(fragment, excluded_ids) or fragment.fragment_id in superseded:
            continue
        digest = hashlib.sha256(
            (fragment.fragment_id + ":feature-excluded:" + snapshot_id).encode()
        ).hexdigest()
        retired.append(replace(fragment, fragment_id="fragment_" + digest[:16],
            dependency_fingerprint=digest, status="stale", supersedes=fragment.fragment_id,
            blocking_reasons=[{"code": "feature_excluded"}]))
    return retired
