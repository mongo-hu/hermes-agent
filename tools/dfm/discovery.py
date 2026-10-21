"""Deterministic discovery orchestration for immutable v2 feature snapshots."""

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
    ResolvedArgument,
)
from .errors import DFMError
from .feature_recognition import (
    OCCTCppFeatureRecognitionProvider,
)
from .ontology import LocalOntologyStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _content_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_coverage(raw: Any) -> list[dict[str, Any]]:
    """Validate the public discovery-coverage rows used by planning gates."""

    if not isinstance(raw, list):
        raise DFMError(
            "geometry_protocol_invalid",
            "Feature discovery coverage must be an array.",
        )
    rows: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DFMError(
                "geometry_protocol_invalid",
                "Feature discovery returned a non-object coverage row.",
            )
        feature_kind = str(item.get("feature_kind") or "")
        context = item.get("context", {})
        status = str(item.get("status") or "")
        if not feature_kind or not isinstance(context, dict) or status not in {
            "complete",
            "partial",
            "failed",
        }:
            raise DFMError(
                "geometry_protocol_invalid",
                "Feature discovery returned an invalid coverage row.",
                {"feature_kind": feature_kind, "status": status},
            )
        identity = (
            feature_kind,
            json.dumps(context, sort_keys=True, separators=(",", ":")),
        )
        if identity in identities:
            raise DFMError(
                "geometry_protocol_invalid",
                "Feature discovery returned duplicate coverage identities.",
                {"feature_kind": feature_kind, "context": context},
            )
        identities.add(identity)
        rows.append({
            "feature_kind": feature_kind,
            "context": dict(context),
            "status": status,
            "covered_geometry_ref": item.get("covered_geometry_ref"),
            "unresolved_geometry_ref": item.get("unresolved_geometry_ref"),
            "diagnostics": dict(item.get("diagnostics") or {}),
        })
    return rows


class DiscoveryEngine:
    """Own immutable discovery identities and fail closed without a provider."""

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
        self.geometry_provider = (
            geometry_provider or OCCTCppFeatureRecognitionProvider()
        )
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
        """Run v2 geometry discovery; an unavailable selected backend is an error."""

        geometry_inputs = [
            item
            for item in self.active_inputs(manifest)
            if item.kind in {"step", "parasolid"}
        ]
        features = list(manifest.features)
        regions = list(manifest.regions)
        artifacts = list(manifest.artifacts)
        capabilities = dict(manifest.capabilities)
        discovery_state = dict(capabilities.get("geometry_discovery") or {})
        by_input = dict(discovery_state.get("inputs") or {})
        provider_capability = self.geometry_provider.capability(manifest.project_id)
        provider_capability_signature = _content_hash({
            "contract_version": provider_capability.get("contract_version"),
            "engine_version": provider_capability.get("engine_version"),
            "schema_bundle_version": provider_capability.get("schema_bundle_version"),
            "feature_kinds": provider_capability.get("feature_kinds", []),
            "geometrics": provider_capability.get("geometrics", []),
        })
        if (
            geometry_inputs
            and project_dir is not None
            and provider_capability.get("status") != "available"
        ):
            raise DFMError(
                str(
                    provider_capability.get("error_code")
                    or "geometry_dependency_missing"
                ),
                "The selected OCCT geometry provider cannot establish dfm.geometry/v2.",
                provider_capability,
            )
        if provider_capability.get("status") == "available" and project_dir is not None:
            for input_record in geometry_inputs:
                cached_discovery = by_input.get(input_record.sha256, {})
                already_discovered = any(
                    item.input_sha256 == input_record.sha256
                    and item.kind == "main_wall"
                    and item.recognizer == self.geometry_provider.key
                    and item.recognizer_version == self.geometry_provider.version
                    for item in features
                )
                if (
                    already_discovered
                    and cached_discovery.get("capability_signature")
                    == provider_capability_signature
                ):
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
                    "process",
                    ResolvedArgument(
                        value=manifest.process or "injection",
                        source_ref="project:process",
                    ),
                )
                result = self.geometry_provider.recognize(
                    input_record,
                    process=manifest.process or "injection",
                    facts=resolved_facts,
                    project_dir=project_dir,
                )
                replaced_feature_ids = {item.feature_id for item in result.features}
                previous_feature_ids = {
                    item.feature_id
                    for item in features
                    if item.input_sha256 == input_record.sha256
                    and item.recognizer == self.geometry_provider.key
                }
                features = [
                    item
                    for item in features
                    if item.feature_id not in replaced_feature_ids
                    and not (
                        item.input_sha256 == input_record.sha256
                        and item.recognizer == self.geometry_provider.key
                    )
                ] + result.features
                regions = [
                    item
                    for item in regions
                    if not (
                        item.input_sha256 == input_record.sha256
                        and any(
                            ref in previous_feature_ids | replaced_feature_ids
                            for ref in item.feature_refs
                        )
                    )
                ]
                artifact_ids = {item.artifact_id for item in result.artifacts}
                artifacts = [
                    item for item in artifacts if item.artifact_id not in artifact_ids
                ] + result.artifacts
                coverage = _normalize_coverage(result.diagnostics.get("coverage", []))
                by_input[input_record.sha256] = {
                    "provider": self.geometry_provider.key,
                    "provider_version": self.geometry_provider.version,
                    "capability_signature": provider_capability_signature,
                    "topology_snapshot_id": result.topology_snapshot_id,
                    "render_mesh_snapshot_id": result.render_mesh_snapshot_id,
                    "geometry_snapshot_ref": result.geometry_snapshot_ref,
                    "worker_snapshot_id": result.worker_snapshot_id,
                    "artifact_refs": [item.artifact_id for item in result.artifacts],
                    "coverage": coverage,
                }
        discovery_state.update({
            "status": provider_capability.get("status"),
            "inputs": by_input,
        })
        capabilities["geometry_discovery"] = discovery_state
        return replace(
            manifest,
            features=features,
            regions=regions,
            artifacts=artifacts,
            capabilities=capabilities,
            updated_at=_utc_now(),
        )

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
        if any(
            item.properties.get("region_review") == "user_confirmed"
            for item in features
        ):
            identity["region_content_sha256"] = {
                item.region_id: item.content_sha256 for item in regions
            }
        content_sha256 = _content_hash(identity)
        discovery_inputs = refreshed.capabilities.get("geometry_discovery", {}).get(
            "inputs", {}
        )
        geometry_metadata = (
            discovery_inputs.get(active[0].sha256, {}) if len(active) == 1 else {}
        )
        identity["worker_snapshot_id"] = geometry_metadata.get(
            "worker_snapshot_id", ""
        )
        identity["feature_geometry"] = {
            item.feature_id: item.properties.get("geometry_refs", [])
            for item in features
        }
        identity["coverage"] = list(geometry_metadata.get("coverage", []))
        content_sha256 = _content_hash(identity)
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
            worker_snapshot_id=geometry_metadata.get("worker_snapshot_id", ""),
            coverage=list(geometry_metadata.get("coverage", [])),
        )
        return (
            replace(
                refreshed,
                discovery_snapshots=[*refreshed.discovery_snapshots, snapshot],
                updated_at=_utc_now(),
            ),
            snapshot,
        )

    def provider_versions(self, project_id: str = "") -> dict[str, str]:
        versions = {
            "hermes_discovery": self.version,
            "drawing": self.drawing_provider_version,
            "drawing_geometry_fusion": self.fusion_provider_version,
        }
        versions.update({
            provider.key: (
                f"{provider.version}:{provider.capability(project_id).get('status')}"
            )
            for provider in self.geometry_providers
        })
        return versions

    def capability(self, project_id: str = "") -> dict[str, Any]:
        provider_capabilities = [
            provider.capability(project_id) for provider in self.geometry_providers
        ]
        provider_status = (
            provider_capabilities[0].get("status")
            if provider_capabilities
            else "dependency_missing"
        )
        return {
            "status": provider_status,
            "catalog_id": self.catalog["catalog_id"],
            "catalog_version": self.catalog["version"],
            "providers": self.provider_versions(project_id),
            "provider_capabilities": provider_capabilities,
            "recognizers": self.catalog["recognizers"],
            "placeholder_policy": self.catalog["placeholder_policy"],
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
            policy.get("behavior") != "fail_closed"
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
