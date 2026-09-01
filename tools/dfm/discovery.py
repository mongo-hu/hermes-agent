"""Deterministic discovery orchestration with an honest ordinary-region fallback."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import (
    DiscoverySnapshotRecord,
    FeatureRecord,
    InputRecord,
    ProjectManifest,
    RegionRecord,
    GeometryRef,
    ResolvedArgument,
)
from .errors import DFMError
from .feature_recognition import (
    OCCTCppFeatureRecognitionProvider,
)
from .ontology import LocalOntologyStore


FALLBACK_RECOGNIZER = "ordinary-region-fallback"
FALLBACK_VERSION = "1.0.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _content_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class DiscoveryEngine:
    """Own immutable discovery identities and the ordinary-region fallback."""

    version = "hermes-discovery-v2"

    def __init__(
        self,
        catalog_path: Path | None = None,
        ontology_store: LocalOntologyStore | None = None,
        drawing_provider_version: str = "not_configured",
        fusion_provider_version: str = "not_configured",
        geometry_provider: OCCTCppFeatureRecognitionProvider | None = None,
    ) -> None:
        self.catalog_path = catalog_path or (
            Path(__file__).resolve().parent
            / "scopes"
            / "injection"
            / "feature_catalog.json"
        )
        self.catalog = self._load_catalog()
        self.ontology_store = ontology_store
        self.drawing_provider_version = drawing_provider_version
        self.fusion_provider_version = fusion_provider_version
        self.geometry_provider = geometry_provider or OCCTCppFeatureRecognitionProvider()
        self.geometry_providers = (self.geometry_provider,)

    @staticmethod
    def active_inputs(manifest: ProjectManifest) -> list[InputRecord]:
        superseded = {
            item.supersedes_input_id
            for item in manifest.inputs
            if item.supersedes_input_id
        }
        return [item for item in manifest.inputs if item.input_id not in superseded]

    def refresh_candidates(
        self, manifest: ProjectManifest, *, project_dir: Path | None = None
    ) -> ProjectManifest:
        """Run available geometry discovery, then maintain the honest fallback."""

        geometry_inputs = [
            item
            for item in self.active_inputs(manifest)
            if item.kind in {"step", "parasolid"}
        ]
        active_hashes = {item.sha256 for item in geometry_inputs}
        features = [
            item
            for item in manifest.features
            if item.recognizer != FALLBACK_RECOGNIZER
            or item.input_sha256 in active_hashes
        ]
        regions = [
            item
            for item in manifest.regions
            if not item.source_refs
            or not item.source_refs[0].startswith(f"recognizer:{FALLBACK_RECOGNIZER}")
            or item.input_sha256 in active_hashes
        ]
        artifacts = list(manifest.artifacts)
        capabilities = dict(manifest.capabilities)
        discovery_state = dict(capabilities.get("geometry_discovery") or {})
        by_input = dict(discovery_state.get("inputs") or {})
        provider_capability = self.geometry_provider.capability()
        if provider_capability.get("status") == "available" and project_dir is not None:
            for input_record in geometry_inputs:
                already_discovered = any(
                    item.input_sha256 == input_record.sha256
                    and item.kind == "main_wall"
                    and item.recognizer == self.geometry_provider.key
                    and item.recognizer_version == self.geometry_provider.version
                    for item in features
                )
                if already_discovered and input_record.sha256 in by_input:
                    continue
                resolved_facts = {
                    item.name: ResolvedArgument(
                        value=item.value,
                        source_ref=f"fact:{item.fact_id}",
                        unit=item.unit,
                    )
                    for item in manifest.facts
                    if item.status == "confirmed"
                }
                resolved_facts.setdefault(
                    "process", ResolvedArgument(
                        value=manifest.process or "injection",
                        source_ref="project:process",
                    )
                )
                result = self.geometry_provider.recognize(
                    input_record,
                    process=manifest.process or "injection",
                    facts=resolved_facts,
                    project_dir=project_dir,
                )
                replaced_feature_ids = {item.feature_id for item in result.features}
                replaced_region_ids = {item.region_id for item in result.regions}
                features = [
                    item for item in features
                    if item.feature_id not in replaced_feature_ids
                    and not (
                        item.input_sha256 == input_record.sha256
                        and item.recognizer == self.geometry_provider.key
                    )
                ] + result.features
                regions = [
                    item for item in regions
                    if item.region_id not in replaced_region_ids
                    and not (
                        item.input_sha256 == input_record.sha256
                        and any(ref in replaced_feature_ids for ref in item.feature_refs)
                    )
                ] + result.regions
                artifact_ids = {item.artifact_id for item in result.artifacts}
                artifacts = [
                    item for item in artifacts if item.artifact_id not in artifact_ids
                ] + result.artifacts
                by_input[input_record.sha256] = {
                    "provider": self.geometry_provider.key,
                    "provider_version": self.geometry_provider.version,
                    "topology_snapshot_id": result.topology_snapshot_id,
                    "render_mesh_snapshot_id": result.render_mesh_snapshot_id,
                    "geometry_snapshot_ref": result.geometry_snapshot_ref,
                    "artifact_refs": [item.artifact_id for item in result.artifacts],
                }
        discovery_state.update(
            {"status": provider_capability.get("status"), "inputs": by_input}
        )
        capabilities["geometry_discovery"] = discovery_state
        feature_ids = {item.feature_id for item in features}
        region_ids = {item.region_id for item in regions}
        for input_record in geometry_inputs:
            suffix = input_record.sha256[:16]
            feature_id = f"feature.ordinary.{suffix}"
            region_id = f"region.ordinary.{suffix}"
            source_refs = [
                f"recognizer:{FALLBACK_RECOGNIZER}@{FALLBACK_VERSION}",
                f"input:{input_record.input_id}",
            ]
            if region_id not in region_ids:
                identity = {
                    "region_id": region_id,
                    "input_sha256": input_record.sha256,
                    "mode": "whole_model",
                    "role": "ordinary",
                    "feature_refs": [feature_id],
                }
                regions.append(
                    RegionRecord(
                        region_id=region_id,
                        input_sha256=input_record.sha256,
                        coordinate_system="model",
                        mode="whole_model",
                        semantic_label="ordinary_model_region",
                        source_refs=source_refs,
                        version=FALLBACK_VERSION,
                        content_sha256=_content_hash(identity),
                        role="ordinary",
                        feature_refs=[feature_id],
                    )
                )
                region_ids.add(region_id)
            if feature_id not in feature_ids:
                features.append(
                    FeatureRecord(
                        feature_id=feature_id,
                        kind="ordinary_part",
                        source_refs=source_refs,
                        confidence=1.0,
                        input_sha256=input_record.sha256,
                        region_refs=[region_id],
                        properties={
                            "fallback": True,
                            "coverage": "whole_model",
                            "requested_feature_recognizers": [
                                item["recognizer_id"]
                                for item in self.catalog["recognizers"]
                                if item["status"] == "placeholder"
                            ],
                        },
                        recognizer=FALLBACK_RECOGNIZER,
                        recognizer_version=FALLBACK_VERSION,
                        status="confirmed",
                    )
                )
                feature_ids.add(feature_id)
        regions = self._partition_ordinary_regions(
            features, regions, manifest.process or "injection"
        )
        return replace(
            manifest,
            features=features,
            regions=regions,
            artifacts=artifacts,
            capabilities=capabilities,
            updated_at=_utc_now(),
        )

    @staticmethod
    def _geometry_key(ref: GeometryRef) -> tuple[str, int, str]:
        return ref.kind, ref.index, ref.input_sha256

    def _partition_ordinary_regions(
        self,
        features: list[FeatureRecord],
        regions: list[RegionRecord],
        process: str,
    ) -> list[RegionRecord]:
        """Turn whole-model fallback into the complement of concrete feature faces."""

        feature_by_id = {item.feature_id: item for item in features}
        metric_bindings = {
            (item["feature_kind"], item["region_role"])
            for item in self._metric_bindings(process)
            if item.get("status") in {"available", "placeholder", "released"}
        }
        claimed_by_input: dict[str, dict[tuple[str, int, str], GeometryRef]] = {}
        for region in regions:
            if region.role == "ordinary":
                continue
            concrete_features = [
                feature_by_id[ref]
                for ref in region.feature_refs
                if ref in feature_by_id
                and feature_by_id[ref].kind != "ordinary_part"
                and feature_by_id[ref].status in {"confirmed", "detected"}
            ]
            if not concrete_features:
                continue
            if not any(
                (feature.kind, region.role) in metric_bindings
                for feature in concrete_features
            ):
                continue
            if region.mode != "topology_refs" or not region.geometry_refs:
                raise DFMError(
                    "feature_region_not_computable",
                    "A concrete feature region must resolve to immutable topology refs.",
                    {"region_id": region.region_id, "mode": region.mode},
                )
            claimed = claimed_by_input.setdefault(region.input_sha256, {})
            for ref in region.geometry_refs:
                if ref.input_sha256 != region.input_sha256:
                    raise DFMError(
                        "feature_region_not_computable",
                        "Feature region topology belongs to another geometry input.",
                        {"region_id": region.region_id},
                    )
                claimed[self._geometry_key(ref)] = ref

        output = []
        for region in regions:
            if region.role != "ordinary":
                output.append(region)
                continue
            excluded = sorted(
                claimed_by_input.get(region.input_sha256, {}).values(),
                key=self._geometry_key,
            )
            identity = {
                "region_id": region.region_id,
                "input_sha256": region.input_sha256,
                "mode": "topology_complement" if excluded else "whole_model",
                "role": "ordinary",
                "feature_refs": region.feature_refs,
                "excluded_geometry_refs": [item.to_dict() for item in excluded],
            }
            output.append(
                replace(
                    region,
                    mode=identity["mode"],
                    excluded_geometry_refs=excluded,
                    content_sha256=_content_hash(identity),
                )
            )
        return output

    def analysis_targets(
        self, manifest: ProjectManifest, snapshot: DiscoverySnapshotRecord
    ) -> list[dict[str, Any]]:
        """Resolve one non-overlapping region target for each supported metric."""

        features = {
            item.feature_id: item
            for item in manifest.features
            if item.feature_id in snapshot.feature_refs
        }
        bindings = self._metric_bindings(manifest.process or "injection")
        main_wall_inputs = {
            item.input_sha256 for item in features.values()
            if item.kind == "main_wall" and item.status in {"confirmed", "detected"}
        }
        targets: list[dict[str, Any]] = []
        claims: dict[tuple[str, tuple[str, int, str]], str] = {}
        for region in manifest.regions:
            if region.region_id not in snapshot.region_refs:
                continue
            matching_features = [
                features[ref] for ref in region.feature_refs if ref in features
            ]
            if len(matching_features) != 1:
                raise DFMError(
                    "analysis_region_invalid",
                    "Every analysis region must belong to exactly one feature.",
                    {"region_id": region.region_id},
                )
            feature = matching_features[0]
            binding = next(
                (
                    item
                    for item in bindings
                    if item["feature_kind"] == feature.kind
                    and item["region_role"] == region.role
                ),
                None,
            )
            if binding is None:
                continue
            for metric_id in binding["metrics"]:
                if (
                    metric_id == "injection.geometry.wall_thickness"
                    and feature.kind == "ordinary_part"
                    and feature.input_sha256 in main_wall_inputs
                ):
                    continue
                for ref in region.geometry_refs:
                    key = (metric_id, self._geometry_key(ref))
                    owner = claims.get(key)
                    if owner is not None and owner != region.region_id:
                        raise DFMError(
                            "analysis_region_overlap",
                            "Two feature regions claim the same topology for one metric.",
                            {
                                "metric_id": metric_id,
                                "region_ids": [owner, region.region_id],
                            },
                        )
                    claims[key] = region.region_id
                targets.append({
                    "feature": feature,
                    "region": region,
                    "metric_id": metric_id,
                    "rule_profile": binding.get("rule_profile"),
                    "fallback_to": binding.get("fallback_to"),
                })
        return targets

    def _metric_bindings(self, process: str) -> list[dict[str, Any]]:
        catalog_bindings = [
            dict(item) for item in self.catalog["feature_metric_bindings"]
        ]
        if self.ontology_store is not None:
            published = self.ontology_store.analysis_target_specs(process)
            if published:
                combined = [dict(item) for item in published]
                keys = {
                    (item["feature_kind"], item["region_role"])
                    for item in combined
                }
                combined.extend(
                    item for item in catalog_bindings
                    if item.get("status") in {"available", "released"}
                    and (item["feature_kind"], item["region_role"]) not in keys
                )
                return combined
        return catalog_bindings

    def freeze(
        self, manifest: ProjectManifest
    ) -> tuple[ProjectManifest, DiscoverySnapshotRecord]:
        refreshed = self.refresh_candidates(manifest)
        active = self.active_inputs(refreshed)
        input_hashes = {item.input_id: item.sha256 for item in active}
        active_input_ids = set(input_hashes)
        active_hashes = set(input_hashes.values())
        observations = [
            item for item in refreshed.observations if item.input_id in active_input_ids
        ]
        observation_ids = {item.observation_id for item in observations}
        features = [
            item for item in refreshed.features if item.input_sha256 in active_hashes
        ]
        feature_ids = {item.feature_id for item in features}
        regions = [
            item
            for item in refreshed.regions
            if item.input_sha256 in active_hashes
            and set(item.feature_refs).issubset(feature_ids)
        ]
        region_ids = {item.region_id for item in regions}
        fusion_links = [
            item
            for item in refreshed.fusion_links
            if set(item.observation_refs).issubset(observation_ids)
            and set(item.feature_refs).issubset(feature_ids)
            and set(item.region_refs).issubset(region_ids)
        ]
        discovery_fact_names = {"process", *self.required_fact_names()}
        facts = [
            item
            for item in refreshed.facts
            if item.status == "confirmed" and item.name in discovery_fact_names
        ]
        identity = {
            "input_hashes": input_hashes,
            "process": refreshed.process,
            "confirmed_fact_refs": [item.fact_id for item in facts],
            "observation_refs": [item.observation_id for item in observations],
            "feature_refs": [item.feature_id for item in features],
            "region_refs": [item.region_id for item in regions],
            "fusion_link_refs": [item.fusion_link_id for item in fusion_links],
            "provider_versions": self.provider_versions(),
        }
        content_sha256 = _content_hash(identity)
        discovery_inputs = (
            refreshed.capabilities.get("geometry_discovery", {}).get("inputs", {})
        )
        geometry_metadata = (
            discovery_inputs.get(active[0].sha256, {}) if len(active) == 1 else {}
        )
        existing = next(
            (
                item
                for item in reversed(refreshed.discovery_snapshots)
                if item.content_sha256 == content_sha256
            ),
            None,
        )
        if existing is not None:
            return refreshed, existing
        snapshot = DiscoverySnapshotRecord(
            snapshot_id=f"discovery.snapshot.{content_sha256[:16]}",
            created_at=_utc_now(),
            input_hashes=input_hashes,
            observation_refs=identity["observation_refs"],
            feature_refs=identity["feature_refs"],
            region_refs=identity["region_refs"],
            fusion_link_refs=identity["fusion_link_refs"],
            provider_versions=identity["provider_versions"],
            content_sha256=content_sha256,
            process=refreshed.process,
            confirmed_fact_refs=identity["confirmed_fact_refs"],
            geometry_snapshot_ref=geometry_metadata.get("geometry_snapshot_ref", ""),
            topology_snapshot_id=geometry_metadata.get("topology_snapshot_id", ""),
            render_mesh_snapshot_id=geometry_metadata.get(
                "render_mesh_snapshot_id", ""
            ),
            artifact_refs=list(geometry_metadata.get("artifact_refs", [])),
        )
        return (
            replace(
                refreshed,
                discovery_snapshots=[*refreshed.discovery_snapshots, snapshot],
                updated_at=_utc_now(),
            ),
            snapshot,
        )

    def provider_versions(self) -> dict[str, str]:
        versions = {
            "hermes_discovery": self.version,
            "ordinary_region": FALLBACK_VERSION,
            "drawing": self.drawing_provider_version,
            "drawing_geometry_fusion": self.fusion_provider_version,
        }
        versions.update({
            provider.key: f"{provider.version}:{provider.capability().get('status')}"
            for provider in self.geometry_providers
        })
        return versions

    def capability(self) -> dict[str, Any]:
        return {
            "status": "available_with_fallback",
            "catalog_id": self.catalog["catalog_id"],
            "catalog_version": self.catalog["version"],
            "providers": self.provider_versions(),
            "provider_capabilities": [
                provider.capability() for provider in self.geometry_providers
            ],
            "recognizers": self.catalog["recognizers"],
            "placeholder_policy": self.catalog["placeholder_policy"],
            "fallback_feature_kind": "ordinary_part",
            "fallback_region_role": "ordinary",
        }

    def _load_catalog(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DFMError(
                "discovery_catalog_invalid",
                "The DFM feature discovery catalog could not be loaded.",
                {"path": str(self.catalog_path)},
            ) from exc
        if (
            payload.get("catalog_id") != "injection.wall-draft.features"
            or not isinstance(payload.get("recognizers"), list)
            or not isinstance(payload.get("feature_metric_bindings"), list)
            or not isinstance(payload.get("placeholder_policy"), dict)
        ):
            raise DFMError(
                "discovery_catalog_invalid",
                "The DFM feature discovery catalog has an invalid contract.",
            )
        for recognizer in payload["recognizers"]:
            if not isinstance(recognizer, dict):
                raise DFMError(
                    "discovery_catalog_invalid",
                    "A feature recognizer declaration is invalid.",
                    {"recognizer": recognizer},
                )
            observation_kinds = recognizer.get("observation_kinds", [])
            if (
                not recognizer.get("recognizer_id")
                or (not recognizer.get("feature_kind") and not observation_kinds)
                or not isinstance(observation_kinds, list)
                or any(not item for item in observation_kinds)
                or not isinstance(recognizer.get("region_roles"), list)
                or not isinstance(recognizer.get("required_fact_names"), list)
                or recognizer.get("status") not in {"available", "placeholder"}
            ):
                raise DFMError(
                    "discovery_catalog_invalid",
                    "A feature recognizer declaration is invalid.",
                    {"recognizer": recognizer},
                )
        policy = payload["placeholder_policy"]
        if (
            policy.get("behavior") != "treat_as_ordinary"
            or policy.get("fallback_feature_kind") != "ordinary_part"
            or policy.get("fallback_region_role") != "ordinary"
            or policy.get("emit_synthetic_process_features") is not False
        ):
            raise DFMError(
                "discovery_catalog_invalid",
                "The placeholder feature fallback policy is invalid.",
            )
        return payload

    def required_fact_names(self) -> set[str]:
        """Facts needed by recognizers that can actually run in this release."""

        return {
            str(name)
            for recognizer in self.catalog["recognizers"]
            if recognizer["status"] == "available"
            for name in recognizer["required_fact_names"]
        }
