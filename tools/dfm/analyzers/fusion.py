"""Deterministic, reviewable links from drawing observations to 3D regions."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from ..contracts import (
    WORKER_SCHEMA_VERSION,
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    FusionLinkRecord,
    ProjectManifest,
    WorkerEvent,
)
from ..errors import DFMError
from ..project.manifest import ManifestStore
from .base import AnalyzerContext, CancellationToken


_GLOBAL_OBSERVATION_KINDS = {
    "general_tolerance",
    "global_note",
    "manufacturing_constraint",
    "material",
    "part_name",
    "surface_finish",
    "thread_requirement",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FusionAnalyzer:
    key = "fusion"
    version = "2.0.0"
    supported_inputs = ("fusion",)

    def capability(self, context: AnalyzerContext) -> Capability:
        has_drawing = any(item.kind == "drawing" for item in context.inputs)
        has_geometry = any(
            item.kind in {"step", "parasolid"} for item in context.inputs
        )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "Reviewable observation-to-feature fusion is available.",
            details={
                "applicable": has_drawing and has_geometry,
                "output_contract": "FusionLinkRecord[]",
                "automatic_confirmation": False,
            },
        )

    def resolve(self, manifest: ProjectManifest) -> list[FusionLinkRecord]:
        feature_by_id = {item.feature_id: item for item in manifest.features}
        region_by_id = {item.region_id: item for item in manifest.regions}
        features_by_kind: dict[str, list[str]] = {}
        regions_by_role: dict[str, list[str]] = {}
        for feature in manifest.features:
            features_by_kind.setdefault(feature.kind, []).append(feature.feature_id)
        for region in manifest.regions:
            regions_by_role.setdefault(region.role, []).append(region.region_id)

        links: list[FusionLinkRecord] = []
        for observation in manifest.observations:
            if observation.status in {"rejected", "conflict"}:
                continue
            provenance = observation.provenance or {}
            explicit_features = [
                item for item in observation.feature_refs if item in feature_by_id
            ]
            explicit_regions = [
                item for item in observation.region_refs if item in region_by_id
            ]
            feature_kind = str(provenance.get("feature_kind") or "")
            region_role = str(provenance.get("region_role") or "")
            feature_refs = explicit_features or list(
                features_by_kind.get(feature_kind, [])
            )
            region_refs = explicit_regions or list(regions_by_role.get(region_role, []))

            if feature_refs and not region_refs:
                region_refs = sorted({
                    region_id
                    for feature_id in feature_refs
                    for region_id in feature_by_id[feature_id].region_refs
                    if region_id in region_by_id
                })
            if region_refs and not feature_refs:
                feature_refs = sorted({
                    feature_id
                    for region_id in region_refs
                    for feature_id in region_by_id[region_id].feature_refs
                    if feature_id in feature_by_id
                })
            if not feature_refs and not region_refs:
                if observation.kind in _GLOBAL_OBSERVATION_KINDS:
                    continue
                continue

            candidate_count = max(len(feature_refs), len(region_refs), 1)
            status = "candidate" if candidate_count == 1 else "ambiguous"
            method = (
                "explicit_observation_reference"
                if explicit_features or explicit_regions
                else "drawing_semantic_target_hint"
            )
            identity: dict[str, Any] = {
                "observation_refs": [observation.observation_id],
                "feature_refs": sorted(feature_refs),
                "region_refs": sorted(region_refs),
                "method": method,
            }
            digest = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            links.append(
                FusionLinkRecord(
                    fusion_link_id=f"fusion.{digest[:16]}",
                    observation_refs=[observation.observation_id],
                    feature_refs=sorted(feature_refs),
                    region_refs=sorted(region_refs),
                    confidence=max(
                        0.0,
                        min(
                            observation.confidence
                            * (1.0 if method.startswith("explicit") else 0.85),
                            1.0,
                        ),
                    ),
                    status=status,
                    method=method,
                    diagnostics={
                        "provider": "hermes_fusion_resolver",
                        "provider_version": self.version,
                        "candidate_count": candidate_count,
                        "feature_kind": feature_kind,
                        "region_role": region_role,
                        "requires_review": True,
                    },
                )
            )
        return links

    def run(
        self,
        context: AnalyzerContext,
        cancellation: CancellationToken,
    ) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        if context.input_mode != "fusion":
            raise DFMError(
                "input_required",
                "FusionLink resolution requires drawing and geometry inputs.",
            )
        manifest = ManifestStore(context.project_dir).load()
        links = self.resolve(manifest)
        output_dir = context.project_dir / "runs" / context.run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "fusion_links.jsonl"
        output_path.write_text(
            "".join(
                json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                for item in links
            ),
            encoding="utf-8",
            newline="\n",
        )
        digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
        artifact = ArtifactRecord(
            artifact_id=f"artifact_fusion-links_{digest[:16]}",
            kind="fusion_links",
            relative_path=(
                Path("runs") / context.run_id / "artifacts" / output_path.name
            ).as_posix(),
            media_type="application/x-ndjson",
            created_at=_utc_now(),
            run_id=context.run_id,
            logical_id=f"fusion-links:{context.run_id}:{self.version}",
            size_bytes=output_path.stat().st_size,
            sha256=digest,
        )
        if context.event_sink is not None:
            context.event_sink(
                WorkerEvent(
                    WORKER_SCHEMA_VERSION,
                    "artifact",
                    kind=artifact.kind,
                    path=output_path.name,
                )
            )
            context.event_sink(
                WorkerEvent(
                    WORKER_SCHEMA_VERSION,
                    "completed",
                    stage="drawing_geometry_fusion",
                    percent=100,
                )
            )
        return [artifact]
