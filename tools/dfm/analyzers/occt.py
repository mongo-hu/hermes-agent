"""Persistent OCCT analyzer for the public dfm.geometry/v2 session contract."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

from ..contracts import (
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    GeometricResultRecord,
    WorkerEvent,
)
from ..errors import DFMError
from ..runtime.process import GeometrySession, SESSION_CONTRACT
from .base import AnalyzerContext, CancellationToken


ENGINE_VERSION = "occt-dfm-geometry-1.10.1"
_V2_SCALAR_REQUIRED = {
    "schema_version",
    "contract_version",
    "field_id",
    "snapshot_id",
    "input_sha256",
    "feature_id",
    "geometric_ids",
    "context",
    "unit",
    "scene_ref",
    "topology_map_ref",
    "topology_snapshot_ref",
    "render_mesh_snapshot_ref",
    "interpolation",
    "calculation_context",
    "feature_refs",
    "samples",
    "cells",
    "quality",
}
_V2_SCALAR_ALLOWED = _V2_SCALAR_REQUIRED | {"render_samples"}
_LEGACY_ROUTING_FIELDS = {
    "worker_geometric_id",
    "operation_id",
    "calculator_id",
    "metric_id",
    "metric_ids",
    "quantity_id",
    "region_refs",
    "face_indices",
    "task_id",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_legacy_routing(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value).intersection(_LEGACY_ROUTING_FIELDS)
        if forbidden:
            raise DFMError(
                "geometry_protocol_invalid",
                "A dfm.geometry/v2 payload leaked legacy routing fields.",
                {"fields": sorted(forbidden)},
            )
        for nested in value.values():
            _reject_legacy_routing(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_legacy_routing(nested)


def _validate_scalar_field(
    payload: Any,
    *,
    artifact_id: str,
    snapshot_id: str,
    input_sha256: str,
    submitted_keys: set[tuple[str, str, str]],
) -> None:
    if not isinstance(payload, Mapping):
        raise DFMError(
            "geometry_artifact_invalid",
            "A Worker scalar field is not a JSON object.",
            {"artifact_id": artifact_id},
        )
    missing = _V2_SCALAR_REQUIRED - set(payload)
    unknown = set(payload) - _V2_SCALAR_ALLOWED
    _reject_legacy_routing(payload)
    context = payload.get("context")
    geometric_ids = payload.get("geometric_ids")
    if (
        missing
        or unknown
        or payload.get("schema_version") != 2
        or payload.get("contract_version") != SESSION_CONTRACT
        or payload.get("field_id") != artifact_id
        or payload.get("snapshot_id") != snapshot_id
        or payload.get("input_sha256") != input_sha256
        or not isinstance(payload.get("feature_id"), str)
        or not isinstance(context, Mapping)
        or not isinstance(geometric_ids, list)
        or not geometric_ids
        or len(geometric_ids) != len(set(geometric_ids))
    ):
        raise DFMError(
            "geometry_artifact_invalid",
            "A Worker scalar field violates the pinned dfm.geometry/v2 schema.",
            {
                "artifact_id": artifact_id,
                "missing_fields": sorted(missing),
                "unknown_fields": sorted(unknown),
            },
        )
    context_json = json.dumps(context, sort_keys=True, separators=(",", ":"))
    identity = {
        (str(payload["feature_id"]), str(geometric_id), context_json)
        for geometric_id in geometric_ids
    }
    if not identity.issubset(submitted_keys):
        raise DFMError(
            "geometry_artifact_invalid",
            "A Worker scalar field does not belong to the submitted Feature/G/context set.",
            {"artifact_id": artifact_id},
        )


def discover_geometry_executable(configured: str = "") -> str | None:
    """Resolve explicit config, standard repo installs, then PATH."""

    root = Path(__file__).resolve().parents[3]
    if configured:
        candidate = Path(configured).expanduser()
        candidates = (
            (candidate,) if candidate.is_absolute() else (root / candidate, candidate)
        )
        for item in candidates:
            if item.is_file():
                return str(item.resolve())
        discovered = shutil.which(configured)
        return str(Path(discovered).resolve()) if discovered else None
    names = ("dfm-geometry.exe", "dfm-geometry")
    directories = (
        root / "dfm-geometry-exe" / "windows-x64",
        root / "dfm-geometry" / "out" / "install" / "windows-vcpkg-vs2026-sln" / "bin",
        root / "dfm-geometry" / "out" / "build" / "windows-vcpkg-release" / "Release",
    )
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate.resolve())
    for name in names:
        discovered = shutil.which(name)
        if discovered:
            return str(Path(discovered).resolve())
    return None


def probe_geometry_executable(executable: str) -> dict[str, Any]:
    """Perform the same v2 handshake used by production sessions."""

    session = GeometrySession(executable, request_timeout_seconds=30)
    try:
        return dict(session.capability)
    finally:
        session.close()


class OcctAnalyzer:
    key = "occt_cpp"
    version = ENGINE_VERSION
    supported_inputs = ("step",)

    def __init__(
        self,
        executable: str | None,
        *,
        capability_probe: Callable[[str], dict[str, Any]] | None = None,
        timeout_seconds: float = 900,
        session_pool=None,
        lease_seconds: int = 900,
    ) -> None:
        self.executable = executable
        self.capability_probe = capability_probe or probe_geometry_executable
        self.timeout_seconds = timeout_seconds
        self.session_pool = session_pool
        self.lease_seconds = lease_seconds

    def _capability_payload(self, project_id: str = "") -> dict[str, Any]:
        if self.executable is None:
            raise DFMError(
                "geometry_dependency_missing",
                "The configured OCCT geometry executable is unavailable.",
            )
        if self.session_pool is not None and project_id:
            return dict(self.session_pool.acquire(project_id).session.capability)
        payload = self.capability_probe(self.executable)
        if payload.get("contract_version") != SESSION_CONTRACT:
            raise DFMError(
                "geometry_protocol_invalid",
                "The OCCT geometry Worker does not provide dfm.geometry/v2.",
            )
        return payload

    def capability(self, context: AnalyzerContext) -> Capability:
        if self.executable is None:
            return Capability(
                self.key,
                CapabilityStatus.DEPENDENCY_MISSING,
                "The OCCT geometry executable is not installed or configured.",
                "geometry_dependency_missing",
            )
        try:
            payload = self._capability_payload(context.project_id)
        except DFMError as exc:
            return Capability(
                self.key,
                CapabilityStatus.UNHEALTHY,
                exc.message,
                exc.code,
                exc.details or {},
            )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "The persistent dfm.geometry/v2 Worker is available.",
            details={
                "contract_version": payload["contract_version"],
                "engine_version": payload.get("engine_version"),
                "dependency_fingerprint": payload.get("dependency_fingerprint"),
                "backend": payload.get("backend"),
                "occt_version": payload.get("occt_version"),
                "analysis_situs_version": payload.get("analysis_situs_version"),
                "analysis_situs_commit": payload.get("analysis_situs_commit"),
                "feature_kinds": list(payload.get("feature_kinds", [])),
                "geometric_ids": [
                    item.get("geometric_id")
                    for item in payload.get("geometrics", [])
                    if isinstance(item, Mapping)
                    and item.get("support_status") == "supported"
                ],
            },
        )

    @staticmethod
    def _active_step(context: AnalyzerContext):
        superseded = {
            item.supersedes_input_id
            for item in context.inputs
            if item.supersedes_input_id
        }
        matches = [
            item
            for item in context.inputs
            if item.input_id not in superseded and item.kind == "step"
        ]
        if len(matches) != 1:
            raise DFMError(
                "input_selection_invalid",
                "A v2 geometry run requires exactly one active STEP input.",
                {"count": len(matches)},
            )
        return matches[0]

    def run(
        self,
        context: AnalyzerContext,
        cancellation: CancellationToken,
    ) -> list[ArtifactRecord]:
        if self.session_pool is None:
            raise DFMError(
                "geometry_session_required",
                "OCCT analysis requires the project-owned persistent session pool.",
            )
        if context.plan is None or not context.plan.plan_fragments:
            raise DFMError(
                "plan_fragment_required",
                "OCCT analysis requires immutable Geometric plan fragments.",
            )
        input_record = self._active_step(context)
        output_dir = context.project_dir / "runs" / context.run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        opened = self.session_pool.open_model(
            context.project_id,
            input_path=(context.project_dir / input_record.relative_path).resolve(),
            input_sha256=input_record.sha256,
            artifact_dir=(
                context.project_dir
                / "artifacts"
                / "geometry_sessions"
                / input_record.sha256[:16]
            ),
            lease_seconds=self.lease_seconds,
        )
        runnable_fragments = context.plan.active_plan_fragments(statuses={"ready"})
        live_snapshot_ids = {
            fragment.fragment_id: self.session_pool.resolve_snapshot_id(
                context.project_id, fragment.snapshot_id
            )
            for fragment in runnable_fragments
            if fragment.fragment_id not in context.completed_plan_fragment_refs
        }
        shared_artifacts: list[ArtifactRecord] = []
        raw_open_artifacts = opened.get("artifacts")
        if not isinstance(raw_open_artifacts, Mapping):
            raise DFMError(
                "geometry_protocol_invalid",
                "The live Worker snapshot has no geometry artifacts.",
            )
        for kind in ("preflight", "topology_map", "render_scene"):
            artifact_path = Path(str(raw_open_artifacts.get(kind) or "")).resolve()
            if not artifact_path.is_file() or not artifact_path.is_relative_to(
                context.project_dir.resolve()
            ):
                raise DFMError(
                    "geometry_artifact_invalid",
                    "A live Worker snapshot artifact is outside the owning project.",
                    {"kind": kind},
                )
            shared_artifacts.append(
                ArtifactRecord(
                    artifact_id=f"artifact_{context.run_id}_{kind}",
                    kind=kind,
                    relative_path=artifact_path.relative_to(
                        context.project_dir
                    ).as_posix(),
                    media_type="application/json",
                    created_at=_utc_now(),
                    run_id=context.run_id,
                    size_bytes=artifact_path.stat().st_size,
                    sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                )
            )
        path = output_dir / "geometric_results.json"
        results: list[GeometricResultRecord] = []
        if path.is_file():
            try:
                existing_payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(existing_payload, Mapping)
                    or
                    existing_payload.get("contract_version") != SESSION_CONTRACT
                    or existing_payload.get("run_id") != context.run_id
                    or existing_payload.get("input_sha256") != input_record.sha256
                    or not isinstance(existing_payload.get("results"), list)
                ):
                    raise ValueError("result identity mismatch")
                results = [
                    GeometricResultRecord.from_dict(item)
                    for item in existing_payload["results"]
                ]
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise DFMError(
                    "geometric_result_invalid",
                    "The Run's existing Geometric results cannot be extended safely.",
                    {"run_id": context.run_id},
                ) from exc
        scalar_artifacts: dict[str, ArtifactRecord] = {}
        seen_requests: set[tuple[str, str, str, str]] = set()

        for fragment in runnable_fragments:
            fragment.validate()
            if fragment.fragment_id in context.completed_plan_fragment_refs:
                continue
            live_snapshot_id = live_snapshot_ids[fragment.fragment_id]
            cancellation.raise_if_cancelled()
            items = []
            for item in fragment.items:
                context_json = json.dumps(
                    item.context, sort_keys=True, separators=(",", ":")
                )
                geometric_ids = []
                for geometric_id in item.geometric_ids:
                    key = (
                        fragment.snapshot_id,
                        item.feature_id,
                        geometric_id,
                        context_json,
                    )
                    if key not in seen_requests:
                        seen_requests.add(key)
                        geometric_ids.append(geometric_id)
                if geometric_ids:
                    items.append({
                        "feature_id": item.feature_id,
                        "geometric_ids": geometric_ids,
                        "context": dict(item.context),
                    })
            if not items:
                continue
            submitted_keys = {
                (
                    str(item["feature_id"]),
                    str(geometric_id),
                    json.dumps(item["context"], sort_keys=True, separators=(",", ":")),
                )
                for item in items
                for geometric_id in item["geometric_ids"]
            }
            submitted = self.session_pool.call(
                context.project_id,
                "geometrics.compute",
                {"snapshot_id": live_snapshot_id, "items": items},
            )
            external_job_id = str(submitted["job_id"])
            if context.event_sink is not None:
                context.event_sink(
                    WorkerEvent(
                        schema_version=1,
                        type="progress",
                        stage="geometrics.compute.queued",
                        percent=0,
                        external_job_id=external_job_id,
                    )
                )

            def progress(status: dict[str, Any]) -> None:
                if context.event_sink is None:
                    return
                raw = status.get("progress")
                if not isinstance(raw, Mapping):
                    return
                context.event_sink(
                    WorkerEvent(
                        schema_version=1,
                        type="progress",
                        stage=str(raw.get("stage") or "geometrics.compute"),
                        percent=int(raw.get("percent") or 0),
                        external_job_id=external_job_id,
                    )
                )

            completed = self.session_pool.wait_job(
                context.project_id,
                external_job_id,
                cancellation=cancellation,
                on_progress=progress,
            )
            if completed.get("snapshot_id") != live_snapshot_id:
                raise DFMError(
                    "geometry_snapshot_conflict",
                    "The Worker result does not belong to the fragment snapshot.",
                    {"fragment_id": fragment.fragment_id},
                )
            unknown_completed = set(completed) - {
                "contract_version",
                "snapshot_id",
                "status",
                "results",
                "artifacts",
                "diagnostics",
            }
            raw_results = completed.get("results")
            raw_artifacts = completed.get("artifacts")
            if (
                unknown_completed
                or completed.get("contract_version") != SESSION_CONTRACT
                or completed.get("status") != "completed"
                or not isinstance(raw_results, list)
                or not isinstance(raw_artifacts, list)
            ):
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The Worker returned an invalid Geometric batch envelope.",
                    {"fields": sorted(unknown_completed)},
                )
            returned_keys: set[tuple[str, str, str]] = set()
            for raw in raw_results:
                if not isinstance(raw, Mapping):
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "The Worker returned a non-object Geometric result.",
                    )
                unknown = set(raw) - {
                    "feature_id",
                    "geometric_id",
                    "status",
                    "value",
                    "unit",
                    "context",
                    "evidence",
                    "quality",
                    "reason",
                    "provenance",
                }
                if unknown:
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "The Worker returned undeclared fields in a v2 result.",
                        {"fields": sorted(unknown)},
                    )
                if not isinstance(raw.get("context"), Mapping):
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "A Worker Geometric result has an invalid context.",
                    )
                result_key = (
                    str(raw.get("feature_id") or ""),
                    str(raw.get("geometric_id") or ""),
                    json.dumps(raw["context"], sort_keys=True, separators=(",", ":")),
                )
                if result_key not in submitted_keys or result_key in returned_keys:
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "A Worker Geometric result is unrequested or duplicated.",
                        {
                            "feature_id": result_key[0],
                            "geometric_id": result_key[1],
                        },
                    )
                returned_keys.add(result_key)
                provenance = dict(raw.get("provenance") or {})
                provenance["plan_fragment_ref"] = fragment.fragment_id
                stable = hashlib.sha256(
                    json.dumps(
                        {
                            "snapshot_id": fragment.snapshot_id,
                            "feature_id": raw.get("feature_id"),
                            "geometric_id": raw.get("geometric_id"),
                            "context": raw.get("context") or {},
                            "dependency_fingerprint": provenance.get(
                                "dependency_fingerprint"
                            ),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:24]
                record = GeometricResultRecord(
                    result_id=f"geometric_result_{stable}",
                    snapshot_id=live_snapshot_id,
                    feature_id=str(raw.get("feature_id") or ""),
                    geometric_id=str(raw.get("geometric_id") or ""),
                    status=str(raw.get("status") or "failed"),
                    value=raw.get("value"),
                    unit=raw.get("unit"),
                    context=dict(raw.get("context") or {}),
                    evidence=dict(raw.get("evidence") or {}),
                    quality=(
                        dict(raw["quality"])
                        if isinstance(raw.get("quality"), Mapping)
                        else None
                    ),
                    reason=(
                        str(raw["reason"]) if raw.get("reason") is not None else None
                    ),
                    provenance=provenance,
                    input_sha256=input_record.sha256,
                )
                record.validate()
                results.append(record)
            if returned_keys != submitted_keys:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The Worker omitted one or more submitted Geometric results.",
                    {"missing_count": len(submitted_keys - returned_keys)},
                )
            for raw in raw_artifacts:
                if (
                    not isinstance(raw, Mapping)
                    or raw.get("kind") != "scalar_field"
                    or set(raw) != {"artifact_id", "kind", "path", "sha256"}
                ):
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "The Worker returned an invalid Geometric evidence artifact.",
                    )
                artifact_id = str(raw.get("artifact_id") or "")
                artifact_path = Path(str(raw.get("path") or "")).resolve()
                if (
                    not artifact_id
                    or not artifact_path.is_file()
                    or not artifact_path.is_relative_to(context.project_dir.resolve())
                ):
                    raise DFMError(
                        "geometry_artifact_invalid",
                        "A Worker evidence artifact is outside the owning project.",
                        {"artifact_id": artifact_id},
                    )
                artifact_bytes = artifact_path.read_bytes()
                artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
                if raw.get("sha256") != artifact_sha256:
                    raise DFMError(
                        "geometry_artifact_invalid",
                        "A Worker scalar field content hash does not match its descriptor.",
                        {"artifact_id": artifact_id},
                    )
                try:
                    scalar_payload = json.loads(artifact_bytes)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise DFMError(
                        "geometry_artifact_invalid",
                        "A Worker scalar field is not valid UTF-8 JSON.",
                        {"artifact_id": artifact_id},
                    ) from exc
                _validate_scalar_field(
                    scalar_payload,
                    artifact_id=artifact_id,
                    snapshot_id=live_snapshot_id,
                    input_sha256=input_record.sha256,
                    submitted_keys=submitted_keys,
                )
                scalar_artifacts[artifact_id] = ArtifactRecord(
                    artifact_id=artifact_id,
                    kind="scalar_field",
                    relative_path=artifact_path.relative_to(
                        context.project_dir
                    ).as_posix(),
                    media_type="application/json",
                    created_at=_utc_now(),
                    run_id=context.run_id,
                    logical_id=artifact_id,
                    size_bytes=artifact_path.stat().st_size,
                    sha256=artifact_sha256,
                )

            incremental_path = output_dir / "geometric_results.incremental.json"
            incremental_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "contract_version": SESSION_CONTRACT,
                        "run_id": context.run_id,
                        "input_sha256": input_record.sha256,
                        "results": [item.to_dict() for item in results],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if context.event_sink is not None:
                context.event_sink(
                    WorkerEvent(
                        schema_version=1,
                        type="artifact",
                        kind="geometric_results",
                        path=incremental_path.name,
                        external_job_id=external_job_id,
                    )
                )

        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract_version": SESSION_CONTRACT,
                    "run_id": context.run_id,
                    "input_sha256": input_record.sha256,
                    "results": [item.to_dict() for item in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        result_bytes = path.read_bytes()
        return [
            *shared_artifacts,
            ArtifactRecord(
                artifact_id=f"artifact_{context.run_id}_geometric_results",
                kind="geometric_results",
                relative_path=path.relative_to(context.project_dir).as_posix(),
                media_type="application/json",
                created_at=_utc_now(),
                run_id=context.run_id,
                size_bytes=len(result_bytes),
                sha256=hashlib.sha256(result_bytes).hexdigest(),
            ),
            *scalar_artifacts.values(),
        ]
