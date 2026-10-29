"""Persistent dfm.geometry/v2 feature discovery provider."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Mapping

from .base import FeatureRecognitionResult
from ..analyzers.occt import discover_geometry_executable
from ..contracts import ArtifactRecord, FeatureRecord, InputRecord
from ..errors import DFMError
from ..geometry.artifact_metadata import inspect_geometry_file
from ..runtime.process import SESSION_CONTRACT


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OCCTCppFeatureRecognitionProvider:
    key = "occt_cpp_feature_recognition"
    version = "dfm.geometry-v2-feature-provider-1"

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 900,
        session_pool=None,
        lease_seconds: int = 900,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.session_pool = session_pool
        self.lease_seconds = lease_seconds
        self._capability: dict[str, Any] | None = None

    def capability(self, project_id: str = "") -> dict[str, Any]:
        base: dict[str, Any] = {
            "provider": self.key,
            "version": self.version,
            "deployment": "persistent_external_worker",
            "primary_production_target": True,
            "supported_formats": ["step"],
            "required_fact_names": ["process", "model_units", "pull_dir"],
            "output_contracts": ["FeatureRecord[]", SESSION_CONTRACT],
        }
        if self.executable is None:
            return {**base, "status": "dependency_missing"}
        if self.session_pool is None:
            return {
                **base,
                "status": "unhealthy",
                "error_code": "geometry_session_required",
            }
        if not project_id and self._capability is None:
            return {
                **base,
                "status": "unhealthy",
                "error_code": "geometry_project_required",
            }
        if self._capability is None:
            try:
                self._capability = dict(
                    self.session_pool.acquire(project_id).session.capability
                )
            except DFMError as exc:
                return {**base, "status": "unhealthy", "error_code": exc.code}
        return {
            **base,
            "status": "available",
            "contract_version": self._capability["contract_version"],
            "engine_version": self._capability.get("engine_version"),
            "schema_bundle_version": self._capability.get("schema_bundle_version"),
            "methods": dict(self._capability.get("methods") or {}),
            "feature_kinds": list(self._capability.get("feature_kinds", [])),
            "annotation_kinds": list(self._capability.get("annotation_kinds", [])),
            "coverage_statuses": list(self._capability.get("coverage_statuses", [])),
            "geometrics": list(self._capability.get("geometrics", [])),
        }

    def _artifact(
        self, project_dir: Path, input_sha256: str, kind: str, raw_path: object
    ) -> ArtifactRecord:
        path = Path(str(raw_path)).resolve()
        if not path.is_file() or not path.is_relative_to(project_dir):
            raise DFMError(
                "geometry_artifact_invalid",
                "The Worker returned an artifact outside the owning DFM project.",
                {"kind": kind},
            )
        info = inspect_geometry_file(path, kind)
        return ArtifactRecord(
            artifact_id=f"artifact_geometry_{input_sha256[:16]}_{kind}",
            kind=kind,
            relative_path=path.relative_to(project_dir).as_posix(),
            media_type="application/json",
            created_at=_utc_now(),
            size_bytes=info.size_bytes,
            sha256=info.sha256,
        )

    def _feature_records(
        self,
        raw_features: object,
        *,
        input_record: InputRecord,
        snapshot_id: str,
        relationships: list[dict[str, Any]] | None = None,
    ) -> list[FeatureRecord]:
        if not isinstance(raw_features, list):
            raise DFMError(
                "geometry_protocol_invalid",
                "Feature discovery omitted its feature array.",
            )
        features: list[FeatureRecord] = []
        for raw in raw_features:
            if not isinstance(raw, Mapping):
                raise DFMError(
                    "geometry_protocol_invalid",
                    "Feature discovery returned a non-object feature.",
                )
            feature_id = str(raw.get("feature_id") or "")
            feature_kind = str(raw.get("kind") or "")
            if not feature_id or not feature_kind:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "Feature discovery returned an unidentifiable feature.",
                )
            features.append(
                FeatureRecord(
                    feature_id=feature_id,
                    kind=feature_kind,
                    source_refs=[
                        f"worker_snapshot:{snapshot_id}",
                        f"input:{input_record.input_id}",
                    ],
                    confidence=float(raw.get("confidence", 0.0)),
                    input_sha256=input_record.sha256,
                    properties={
                        "subtype": raw.get("subtype"),
                        "parameters": dict(raw.get("parameters") or {}),
                        "parts": dict(raw.get("parts") or {}),
                        "geometry_refs": list(raw.get("geometry_refs") or []),
                        "method": raw.get("method"),
                        "diagnostics": dict(raw.get("diagnostics") or {}),
                        "worker_snapshot_id": snapshot_id,
                    },
                    relationships=(
                        [
                            dict(item)
                            for item in relationships
                            if item.get("from_ref") == feature_id
                        ]
                        if relationships is not None
                        else list(raw.get("relationships") or [])
                    ),
                    recognizer=self.key,
                    recognizer_version=self.version,
                    status="detected",
                )
            )
        return features

    def project_snapshot(
        self, input_record: InputRecord, result: Mapping[str, Any]
    ) -> FeatureRecognitionResult:
        """Build a read projection of the Worker's committed semantic graph."""
        snapshot_id = str(result.get("snapshot_id") or "")
        if not snapshot_id:
            raise DFMError(
                "geometry_protocol_invalid", "The Worker omitted its snapshot identity."
            )
        collections: dict[str, list[dict[str, Any]]] = {}
        for name in ("annotations", "relationships", "forming_scopes", "coverage"):
            rows = result.get(name, [])
            if not isinstance(rows, list) or any(
                not isinstance(row, Mapping) for row in rows
            ):
                raise DFMError(
                    "geometry_protocol_invalid",
                    f"Worker {name} must be an object array.",
                )
            collections[name] = [dict(row) for row in rows]
        return FeatureRecognitionResult(
            features=self._feature_records(
                result.get("features"),
                input_record=input_record,
                snapshot_id=snapshot_id,
                relationships=collections["relationships"],
            ),
            worker_snapshot_id=snapshot_id,
            diagnostics={
                "coverage": collections["coverage"],
                "base_snapshot_id": result.get("base_snapshot_id"),
            },
            annotations=collections["annotations"],
            relationships=collections["relationships"],
            forming_scopes=collections["forming_scopes"],
        )

    @staticmethod
    def _discovery_batches(feature_kinds: list[str]) -> list[list[str]]:
        return [list(feature_kinds)] if feature_kinds else []

    def recognize(
        self,
        input_record: InputRecord,
        *,
        process: str,
        facts: Mapping[str, Any] | None = None,
        project_dir: Path | None = None,
    ) -> FeatureRecognitionResult:
        if process != "injection" or input_record.kind != "step":
            raise DFMError(
                "unsupported_capability",
                "The OCCT v2 feature provider supports injection STEP inputs only.",
            )
        if project_dir is None:
            raise DFMError(
                "discovery_workspace_required",
                "Feature discovery requires the owning DFM project directory.",
            )
        if self.session_pool is None:
            raise DFMError(
                "geometry_session_required",
                "Feature discovery requires the project-owned persistent session pool.",
            )
        project_dir = project_dir.resolve()
        confirmed_direction = (facts or {}).get("pull_dir")
        draw_direction = getattr(confirmed_direction, "value", confirmed_direction)
        if (
            not isinstance(draw_direction, list)
            or len(draw_direction) != 3
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in draw_direction
            )
            or sum(value * value for value in draw_direction) <= 1e-24
        ):
            raise DFMError(
                "direction_required",
                "First-round discovery requires a confirmed main draw direction.",
            )
        capability = self.capability(project_dir.name)
        if capability["status"] != "available":
            raise DFMError(
                capability.get("error_code", "geometry_dependency_missing"),
                "The dfm.geometry/v2 feature provider is unavailable.",
                capability,
            )
        input_path = (project_dir / input_record.relative_path).resolve()
        if not input_path.is_relative_to(project_dir) or not input_path.is_file():
            raise DFMError("input_missing", "The discovery STEP input is unavailable.")
        artifact_dir = (
            project_dir / "artifacts" / "geometry_sessions" / input_record.sha256[:16]
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        project_id = project_dir.name

        opened = self.session_pool.open_model(
            project_id,
            input_path=input_path,
            input_sha256=input_record.sha256,
            artifact_dir=artifact_dir,
            lease_seconds=self.lease_seconds,
        )
        submissions = [
            self.session_pool.call(
                project_id,
                "features.discover",
                {
                    "snapshot_id": opened["snapshot_id"],
                    "feature_kinds": batch,
                    "context": {},
                },
            )
            for batch in self._discovery_batches(capability["feature_kinds"])
        ]
        branches = [
            self.session_pool.wait_job(project_id, submitted["job_id"])
            for submitted in submissions
        ]
        submitted_scope = self.session_pool.call(
            project_id,
            "forming_scopes.analyze",
            {
                "snapshot_id": opened["snapshot_id"],
                "forming_scope_id": "MAIN",
                "mode": "main",
                "draw_direction": list(draw_direction),
                "coordinate_system": "model",
                "direction_source": "user_confirmed",
            },
        )
        branches.append(
            self.session_pool.wait_job(project_id, submitted_scope["job_id"])
        )
        result = (
            branches[0]
            if len(branches) == 1
            else self.session_pool.call(
                project_id,
                "snapshots.merge",
                {
                    "base_snapshot_id": opened["snapshot_id"],
                    "snapshot_ids": [item["snapshot_id"] for item in branches],
                },
            )
        )
        result = self.session_pool.update_snapshot(
            project_id, input_record.sha256, opened, result
        )

        projection = self.project_snapshot(input_record, result)

        opened_artifacts = opened.get("artifacts")
        if not isinstance(opened_artifacts, Mapping):
            raise DFMError(
                "geometry_protocol_invalid",
                "Opening the Worker snapshot did not return geometry artifacts.",
            )
        artifacts = [
            self._artifact(
                project_dir, input_record.sha256, kind, opened_artifacts[kind]
            )
            for kind in ("preflight", "topology_map", "render_scene")
        ]
        return FeatureRecognitionResult(
            features=projection.features,
            diagnostics={
                "contract_version": SESSION_CONTRACT,
                "base_snapshot_id": result.get("base_snapshot_id"),
                "worker_snapshot_id": result["snapshot_id"],
                "coverage": list(result.get("coverage") or []),
                "model_import_count": opened.get("diagnostics", {}).get(
                    "model_import_count"
                ),
            },
            artifacts=artifacts,
            topology_snapshot_id=str(opened_artifacts["topology_snapshot_id"]),
            render_mesh_snapshot_id=str(opened_artifacts["render_mesh_snapshot_id"]),
            geometry_snapshot_ref=artifacts[0].artifact_id,
            worker_snapshot_id=str(result["snapshot_id"]),
            annotations=projection.annotations,
            relationships=projection.relationships,
            forming_scopes=projection.forming_scopes,
        )

    def recognize_scope(
        self,
        input_record: InputRecord,
        *,
        snapshot_id: str,
        forming_scope_id: str,
        project_dir: Path,
    ) -> FeatureRecognitionResult:
        """Supplement one confirmed local scope without rerunning model-wide discovery."""

        if self.session_pool is None:
            raise DFMError(
                "geometry_session_required",
                "Scoped feature discovery requires the project-owned session.",
            )
        capability = self.capability(project_dir.name)
        feature_kinds = list(capability.get("feature_kinds") or [])
        if capability.get("status") != "available" or not feature_kinds:
            raise DFMError(
                "geometry_capability_missing",
                "Scoped feature discovery has no available Feature capability.",
            )
        project_dir = project_dir.resolve()
        opened = self.session_pool.open_model(
            project_dir.name,
            input_path=(project_dir / input_record.relative_path).resolve(),
            input_sha256=input_record.sha256,
            artifact_dir=(
                project_dir
                / "artifacts"
                / "geometry_sessions"
                / input_record.sha256[:16]
            ),
            lease_seconds=self.lease_seconds,
        )
        live_snapshot_id = self.session_pool.resolve_snapshot_id(
            project_dir.name, snapshot_id
        )
        submitted = self.session_pool.call(
            project_dir.name,
            "features.discover",
            {
                "snapshot_id": live_snapshot_id,
                "feature_kinds": feature_kinds,
                "context": {"forming_scope_id": forming_scope_id},
            },
        )
        result = self.session_pool.wait_job(project_dir.name, str(submitted["job_id"]))
        result = self.session_pool.update_snapshot(
            project_dir.name, input_record.sha256, opened, result
        )
        return self.project_snapshot(input_record, result)


def build_occt_feature_recognition_provider(
    configured_executable: str = "",
    *,
    timeout_seconds: float = 900,
    session_pool=None,
    lease_seconds: int = 900,
) -> OCCTCppFeatureRecognitionProvider:
    return OCCTCppFeatureRecognitionProvider(
        discover_geometry_executable(configured_executable),
        timeout_seconds=timeout_seconds,
        session_pool=session_pool,
        lease_seconds=lease_seconds,
    )
