"""OCCT-backed Discovery materialization for the stable Hermes DFM workflow."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import (
    ArtifactRecord,
    DiscoverySnapshotRecord,
    FeatureRecord,
    GeometryRef,
    InputRecord,
    PlanOperation,
    ProjectManifest,
    RegionRecord,
    RuleBinding,
)
from .errors import DFMError


DISCOVERY_ENGINE_VERSION = "hermes-occt-discovery-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class DiscoveryEngine:
    """Convert external OCCT feature artifacts into Hermes-owned frozen records."""

    version = DISCOVERY_ENGINE_VERSION

    def __init__(self, scope_path: Path | None = None) -> None:
        scope_path = scope_path or (
            Path(__file__).resolve().parent
            / "scopes"
            / "injection"
            / "geometry_core_v4.json"
        )
        try:
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DFMError(
                "discovery_catalog_invalid",
                "The DFM feature-to-metric bindings cannot be loaded.",
                {"path": str(scope_path)},
            ) from exc
        bindings = scope.get("feature_metric_bindings")
        if not isinstance(bindings, list):
            raise DFMError(
                "discovery_catalog_invalid",
                "The DFM feature-to-metric bindings are missing.",
            )
        self.feature_kinds_by_operation: dict[str, set[str]] = {}
        for item in bindings:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("feature_kind"), str)
                or not item["feature_kind"]
                or not isinstance(item.get("operation_ids"), list)
                or not item["operation_ids"]
            ):
                raise DFMError(
                    "discovery_catalog_invalid",
                    "A DFM feature-to-metric binding is invalid.",
                    {"binding": item},
                )
            for operation_id in item["operation_ids"]:
                self.feature_kinds_by_operation.setdefault(
                    str(operation_id), set()
                ).add(item["feature_kind"])

    @staticmethod
    def _read(project_dir: Path, artifact: ArtifactRecord) -> dict[str, Any]:
        try:
            payload = json.loads(
                (project_dir / artifact.relative_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise DFMError(
                "discovery_artifact_invalid",
                f"The OCCT {artifact.kind} artifact cannot be read.",
            ) from exc
        if not isinstance(payload, dict):
            raise DFMError(
                "discovery_artifact_invalid",
                f"The OCCT {artifact.kind} artifact must be an object.",
            )
        return payload

    def materialize(
        self,
        manifest: ProjectManifest,
        input_record: InputRecord,
        artifacts: list[ArtifactRecord],
        project_dir: Path,
    ) -> tuple[ProjectManifest, DiscoverySnapshotRecord]:
        by_kind = {item.kind: item for item in artifacts}
        required = {"features", "topology_map", "render_mesh"}
        if required - set(by_kind):
            raise DFMError(
                "discovery_artifact_invalid",
                "OCCT Discovery did not produce feature, topology, and render artifacts.",
                {"missing_artifacts": sorted(required - set(by_kind))},
            )
        feature_payload = self._read(project_dir, by_kind["features"])
        topology_payload = self._read(project_dir, by_kind["topology_map"])
        topology_snapshot_id = str(topology_payload.get("map_id") or "")
        if not topology_snapshot_id:
            raise DFMError(
                "discovery_artifact_invalid",
                "OCCT topology artifact has no immutable map identity.",
            )

        features: list[FeatureRecord] = []
        regions: list[RegionRecord] = []
        for raw in feature_payload.get("features", []):
            if not isinstance(raw, dict):
                continue
            provider_feature_id = str(raw.get("feature_id") or "")
            if not provider_feature_id:
                continue
            feature_id = (
                f"feature.occt.{_hash([input_record.sha256, provider_feature_id])[:16]}"
            )
            geometry_refs = [
                GeometryRef(
                    kind=str(item.get("kind") or ""),
                    index=int(item.get("index") or 0),
                    input_sha256=input_record.sha256,
                    topology_snapshot_id=topology_snapshot_id,
                    entity_id=f"{item.get('kind')}:{int(item.get('index') or 0)}",
                )
                for item in raw.get("geometry_refs", [])
                if isinstance(item, dict)
                and item.get("kind") in {"face", "edge", "solid", "vertex"}
                and isinstance(item.get("index"), int)
                and item["index"] > 0
            ]
            face_refs = [item for item in geometry_refs if item.kind == "face"]
            region_refs: list[str] = []
            if face_refs:
                region_id = f"region.feature.{_hash([input_record.sha256, feature_id, [item.index for item in face_refs]])[:16]}"
                region_refs.append(region_id)
                region_identity = {
                    "region_id": region_id,
                    "feature_id": feature_id,
                    "input_sha256": input_record.sha256,
                    "faces": [item.index for item in face_refs],
                    "topology_snapshot_id": topology_snapshot_id,
                }
                regions.append(
                    RegionRecord(
                        region_id=region_id,
                        input_sha256=input_record.sha256,
                        coordinate_system="model",
                        mode="topology_refs",
                        semantic_label=f"{raw.get('kind') or 'feature'}_region",
                        source_refs=[
                            f"artifact:{by_kind['features'].artifact_id}",
                            f"feature:{feature_id}",
                        ],
                        version=self.version,
                        content_sha256=_hash(region_identity),
                        geometry_refs=face_refs,
                        role="feature",
                        feature_refs=[feature_id],
                    )
                )
            features.append(
                FeatureRecord(
                    feature_id=feature_id,
                    kind=str(raw.get("kind") or "unknown"),
                    source_refs=[str(item) for item in raw.get("source_refs", [])],
                    confidence=float(raw.get("confidence") or 0.0),
                    subtype=str(raw.get("subtype") or ""),
                    geometry_refs=geometry_refs,
                    parameters=dict(raw.get("parameters") or {}),
                    method=str(raw.get("method") or ""),
                    algorithm_version=str(raw.get("algorithm_version") or ""),
                    input_sha256=input_record.sha256,
                    quality=dict(raw.get("quality") or {}),
                    diagnostics=dict(raw.get("diagnostics") or {}),
                    region_refs=region_refs,
                    properties={
                        "provider_feature_id": provider_feature_id,
                        "subtype": raw.get("subtype"),
                        "parameters": dict(raw.get("parameters") or {}),
                    },
                    recognizer=str(raw.get("method") or "occt"),
                    recognizer_version=str(raw.get("algorithm_version") or ""),
                    status="detected",
                )
            )

        if not regions:
            suffix = input_record.sha256[:16]
            feature_id = f"feature.ordinary.{suffix}"
            region_id = f"region.ordinary.{suffix}"
            features.append(
                FeatureRecord(
                    feature_id=feature_id,
                    kind="ordinary_part",
                    source_refs=[f"input:{input_record.input_id}"],
                    confidence=1.0,
                    input_sha256=input_record.sha256,
                    region_refs=[region_id],
                    properties={"fallback": True, "coverage": "whole_model"},
                    recognizer="ordinary-region-fallback",
                    recognizer_version=self.version,
                    status="confirmed",
                )
            )
            regions.append(
                RegionRecord(
                    region_id=region_id,
                    input_sha256=input_record.sha256,
                    coordinate_system="model",
                    mode="whole_model",
                    semantic_label="ordinary_model_region",
                    source_refs=[f"input:{input_record.input_id}"],
                    version=self.version,
                    content_sha256=_hash([region_id, input_record.sha256]),
                    role="ordinary",
                    feature_refs=[feature_id],
                )
            )

        identity = {
            "input_hashes": {input_record.input_id: input_record.sha256},
            "process": manifest.process,
            "features": [item.to_dict() for item in features],
            "regions": [item.to_dict() for item in regions],
            "provider_versions": {"occt": features[0].algorithm_version or self.version},
        }
        content_sha256 = _hash(identity)
        existing = next(
            (
                item
                for item in reversed(manifest.discovery_snapshots)
                if item.content_sha256 == content_sha256
            ),
            None,
        )
        if existing is not None:
            return manifest, existing
        confirmed_fact_refs = [
            item.fact_id
            for item in manifest.facts
            if item.status == "confirmed" and item.name in {"process", "model_units"}
        ]
        snapshot = DiscoverySnapshotRecord(
            snapshot_id=f"discovery.snapshot.{content_sha256[:16]}",
            created_at=_utc_now(),
            input_hashes={input_record.input_id: input_record.sha256},
            observation_refs=[],
            feature_refs=[item.feature_id for item in features],
            region_refs=[item.region_id for item in regions],
            fusion_link_refs=[],
            provider_versions=identity["provider_versions"],
            content_sha256=content_sha256,
            process=manifest.process,
            confirmed_fact_refs=confirmed_fact_refs,
            geometry_snapshot_ref=by_kind["preflight"].artifact_id
            if "preflight" in by_kind
            else "",
            topology_snapshot_id=topology_snapshot_id,
            render_mesh_snapshot_id=by_kind["render_mesh"].artifact_id,
            artifact_refs=[item.artifact_id for item in artifacts],
        )
        active_hashes = {item.sha256 for item in self.active_inputs(manifest)}
        next_manifest = replace(
            manifest,
            features=[
                *[item for item in manifest.features if item.input_sha256 not in active_hashes],
                *features,
            ],
            regions=[
                *[item for item in manifest.regions if item.input_sha256 not in active_hashes],
                *regions,
            ],
            discovery_snapshots=[*manifest.discovery_snapshots, snapshot],
            artifacts=[*manifest.artifacts, *artifacts],
            updated_at=_utc_now(),
        )
        return next_manifest, snapshot

    @staticmethod
    def active_inputs(manifest: ProjectManifest) -> list[InputRecord]:
        superseded = {
            item.supersedes_input_id
            for item in manifest.inputs
            if item.supersedes_input_id
        }
        return [item for item in manifest.inputs if item.input_id not in superseded]

    def analysis_targets(
        self,
        manifest: ProjectManifest,
        snapshot: DiscoverySnapshotRecord,
        operations: list[PlanOperation],
        bindings: list[RuleBinding],
    ) -> tuple[list[PlanOperation], list[RuleBinding], list[RegionRecord]]:
        features = {
            item.feature_id: item
            for item in manifest.features
            if item.feature_id in snapshot.feature_refs
        }
        regions = [
            item
            for item in manifest.regions
            if item.region_id in snapshot.region_refs
        ]
        concrete = [
            item
            for item in regions
            if item.mode == "topology_refs" and item.geometry_refs
        ]
        def targets(operation_id: str) -> tuple[list[str], list[str]]:
            allowed = self.feature_kinds_by_operation.get(operation_id, set())
            selected = [
                region
                for region in concrete
                if any(
                    feature_ref in features
                    and features[feature_ref].kind in allowed
                    for feature_ref in region.feature_refs
                )
            ]
            feature_refs = sorted(
                {
                    feature_ref
                    for region in selected
                    for feature_ref in region.feature_refs
                    if feature_ref in features
                }
            )
            return feature_refs, [region.region_id for region in selected]

        enriched_operations = []
        for operation in operations:
            feature_refs, region_refs = targets(operation.operation_id)
            enriched_operations.append(
                replace(
                    operation,
                    feature_refs=feature_refs,
                    region_refs=region_refs,
                )
                if operation.metric_ids
                else operation
            )
        enriched_bindings = []
        for binding in bindings:
            feature_refs, region_refs = targets(binding.operation_id)
            enriched_bindings.append(
                replace(
                    binding,
                    feature_refs=feature_refs,
                    region_refs=region_refs,
                )
            )
        return enriched_operations, enriched_bindings, regions
