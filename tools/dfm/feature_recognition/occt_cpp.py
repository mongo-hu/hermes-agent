"""Production OCCT main-wall discovery adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping

from .base import FeatureRecognitionResult
from ..analyzers.occt import (
    ENGINE_VERSION,
    GEOMETRY_REQUEST_CONTRACT,
    discover_geometry_executable,
    probe_geometry_executable,
)
from ..contracts import (
    DISCOVERY_SCHEMA_VERSION,
    OBJECTIVE_SCHEMA_VERSION,
    WORKER_SCHEMA_VERSION,
    ArtifactRecord,
    FeatureRecord,
    GeometryDiscoveryResultManifest,
    GeometryDiscoveryTaskRequest,
    GeometryRef,
    InputRecord,
    LocalObjectiveWorkerRequest,
    ObjectiveArtifactManifest,
    ObjectiveTaskRequest,
    PlanOperation,
    RecognizerExecutionResult,
    RegionRecord,
    ResolvedArgument,
)
from ..errors import DFMError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class OCCTCppFeatureRecognitionProvider:
    key = "occt_cpp_feature_recognition"
    version = "occt-injection-features-adapter-1.1.0"

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 900,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def capability(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "provider": self.key,
            "version": self.version,
            "deployment": "external_project",
            "primary_production_target": True,
            "supported_formats": ["step"],
            "required_fact_names": ["process", "model_units"],
            "discovery_contract_version": DISCOVERY_SCHEMA_VERSION,
            "recognizer_ids": ["injection-main-wall"],
            "output_contracts": [
                "ObservationRecord[]",
                "FeatureRecord[]",
                "RegionRecord[]",
                "GeometryDiscoveryResultManifest",
            ],
        }
        if self.executable is None:
            return {**base, "status": "dependency_missing"}
        try:
            payload = probe_geometry_executable(self.executable)
        except DFMError as exc:
            return {**base, "status": "unhealthy", "error_code": exc.code}
        operations = {
            str(item.get("operation_id")): item
            for item in payload.get("operations", [])
            if isinstance(item, dict) and item.get("operation_id")
        }
        supported_discovery_ids = {
            str(execution.get("discovery_operation_id"))
            for binding in payload.get("geometric_bindings", [])
            if isinstance(binding, dict)
            and binding.get("support_status") == "supported"
            and isinstance((execution := binding.get("execution")), dict)
        }
        discovery_ids = []
        main_wall_operation = operations.get("recognize_main_wall", {})
        if main_wall_operation and main_wall_operation.get("status") in {
            None,
            "available",
        }:
            # Main-wall discovery predates the Geometric-ID binding contract;
            # keep it available while an older deployed worker is replaced.
            discovery_ids.append("recognize_main_wall")
        if (
            "recognize_screw_boss" in supported_discovery_ids
            and operations.get("recognize_screw_boss", {}).get("status")
            == "available"
        ):
            discovery_ids.append("recognize_screw_boss")
        recognizer_ids = [
            {
                "recognize_main_wall": "injection-main-wall",
                "recognize_screw_boss": "injection-screw-boss",
            }[operation_id]
            for operation_id in discovery_ids
        ]
        compatible = (
            payload.get("engine_version") == ENGINE_VERSION
            and payload.get("status") == "available"
            and "recognize_main_wall" in discovery_ids
        )
        return {
            **base,
            "status": "available" if compatible else "unhealthy",
            "engine_version": payload.get("engine_version"),
            "recognizer_ids": recognizer_ids,
            "discovery_operation_ids": discovery_ids,
        }

    @staticmethod
    def _operation(
        operation_id: str,
        calculator_id: str,
        dependencies: list[str],
        artifact: str,
    ) -> PlanOperation:
        return PlanOperation(
            operation_id=operation_id,
            calculator_id=calculator_id,
            depends_on=dependencies,
            required_artifacts=[artifact],
        )

    def recognize(
        self,
        input_record: InputRecord,
        *,
        process: str,
        facts: Mapping[str, Any] | None = None,
        project_dir: Path | None = None,
    ) -> FeatureRecognitionResult:
        capability = self.capability()
        if capability["status"] != "available":
            raise DFMError(
                capability.get("error_code", "unsupported_capability"),
                "The production OCCT C++ feature recognizer is unavailable.",
                capability,
            )
        if process != "injection" or input_record.kind != "step":
            raise DFMError(
                "unsupported_capability",
                "OCCT injection feature discovery supports STEP inputs only.",
            )
        if project_dir is None:
            raise DFMError(
                "discovery_workspace_required",
                "OCCT injection feature discovery requires the containing DFM project path.",
            )
        project_dir = project_dir.resolve()
        input_path = (project_dir / input_record.relative_path).resolve()
        if not input_path.is_relative_to(project_dir) or not input_path.is_file():
            raise DFMError("input_missing", "The discovery STEP input is unavailable.")

        suffix = input_record.sha256[:16]
        run_id = f"discovery_{suffix}"
        run_dir = project_dir / "runs" / run_id
        output_dir = project_dir / "artifacts" / "geometry_discovery" / suffix
        run_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        discovery_operations = [
            self._operation(
                "recognize_main_wall",
                "recognize_main_wall",
                ["topology.index"],
                "features",
            )
        ]
        if "recognize_screw_boss" in capability.get("discovery_operation_ids", []):
            discovery_operations.append(
                self._operation(
                    "recognize_screw_boss",
                    "recognize_screw_boss",
                    ["topology.aag"],
                    "features",
                )
            )
        task = ObjectiveTaskRequest(
            schema_version=OBJECTIVE_SCHEMA_VERSION,
            run_id=run_id,
            input_sha256=input_record.sha256,
            input_format="step",
            process=process,
            scope_id="injection.geometry-core",
            scope_version="4.0.0",
            operations=[
                self._operation(
                    "geometry.preflight", "geometry_preflight", [], "preflight"
                ),
                self._operation(
                    "topology.index",
                    "index_topology",
                    ["geometry.preflight"],
                    "topology_map",
                ),
                self._operation(
                    "topology.aag",
                    "build_aag",
                    ["topology.index"],
                    "topology_map",
                ),
                *discovery_operations,
            ],
        )
        request = LocalObjectiveWorkerRequest(
            schema_version=WORKER_SCHEMA_VERSION,
            contract_version=GEOMETRY_REQUEST_CONTRACT,
            backend_version=ENGINE_VERSION,
            input_path=str(input_path),
            output_dir=str(output_dir),
            task=task,
        )
        request_path = run_dir / "discovery_request.json"
        request_path.write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        assert self.executable is not None
        try:
            completed = subprocess.run(
                [self.executable, "analyze", "--request", str(request_path)],
                cwd=project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DFMError(
                "geometry_operation_timeout",
                "OCCT injection feature discovery exceeded its process timeout.",
                {"timeout_seconds": self.timeout_seconds},
            ) from exc
        except OSError as exc:
            raise DFMError(
                "geometry_engine_unhealthy",
                "OCCT injection feature discovery could not start.",
            ) from exc
        (run_dir / "discovery.stdout.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        (run_dir / "discovery.stderr.log").write_text(
            completed.stderr, encoding="utf-8"
        )
        if completed.returncode != 0:
            error_code = "feature_recognition_failed"
            error_message = "OCCT injection feature discovery failed."
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "error":
                    error_code = event.get("code") or error_code
                    error_message = event.get("message") or error_message
            raise DFMError(error_code, error_message)

        topology_path = output_dir / "topology_map.json"
        scene_path = output_dir / "render_scene.json"
        features_path = output_dir / "features.json"
        try:
            topology = json.loads(topology_path.read_text(encoding="utf-8"))
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            native_features = json.loads(
                features_path.read_text(encoding="utf-8")
            )["features"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DFMError(
                "geometry_protocol_invalid",
                "OCCT injection feature discovery artifacts are incomplete.",
            ) from exc
        main_walls = [item for item in native_features if item.get("kind") == "main_wall"]
        if len(main_walls) != 1:
            raise DFMError(
                "feature_recognition_failed",
                "OCCT main-wall discovery must return exactly one main-wall feature.",
            )
        native = main_walls[0]
        topology_snapshot_id = topology["topology_snapshot"]["topology_snapshot_id"]
        render_mesh_snapshot_id = scene["render_mesh_snapshot"][
            "render_mesh_snapshot_id"
        ]
        geometry_refs = [
            GeometryRef.from_dict(item)
            for item in native["geometry_refs"]
            if item.get("kind") == "face"
        ]
        if not geometry_refs or any(
            ref.topology_snapshot_id != topology_snapshot_id
            for ref in geometry_refs
        ):
            raise DFMError(
                "geometry_protocol_invalid",
                "OCCT main-wall refs do not match the topology snapshot.",
            )

        feature_id = native["feature_id"].replace("feature-", "feature.", 1)
        region_id = f"region.main_wall.{suffix}.wall"
        source_refs = [
            f"recognizer:{self.key}@{self.version}",
            f"input:{input_record.input_id}",
            f"native_feature:{native['feature_id']}",
        ]
        region_identity = {
            "region_id": region_id,
            "input_sha256": input_record.sha256,
            "mode": "topology_refs",
            "role": "wall",
            "feature_refs": [feature_id],
            "geometry_refs": [item.to_dict() for item in geometry_refs],
        }
        region = RegionRecord(
            region_id=region_id,
            input_sha256=input_record.sha256,
            coordinate_system="model",
            mode="topology_refs",
            semantic_label="main_wall",
            source_refs=source_refs,
            version=self.version,
            content_sha256=_content_hash(region_identity),
            role="wall",
            feature_refs=[feature_id],
            geometry_refs=geometry_refs,
        )
        feature = FeatureRecord(
            feature_id=feature_id,
            kind="main_wall",
            source_refs=source_refs,
            confidence=float(native["confidence"]),
            input_sha256=input_record.sha256,
            region_refs=[region_id],
            properties={
                **native.get("parameters", {}),
                "method": native.get("method"),
                "quality": native.get("quality", {}),
                "diagnostics": native.get("diagnostics", {}),
            },
            recognizer=self.key,
            recognizer_version=self.version,
            status="detected",
        )
        features = [feature]
        regions = [region]
        recognizers = [
            RecognizerExecutionResult(
                recognizer_id="injection-main-wall",
                status="completed",
                implementation_version=f"{self.version}+{ENGINE_VERSION}",
                feature_refs=[feature_id],
                region_refs=[region_id],
                diagnostics={"native_method": native.get("method")},
            )
        ]

        if "recognize_screw_boss" in capability.get("discovery_operation_ids", []):
            screw_feature_refs: list[str] = []
            screw_region_refs: list[str] = []
            screw_bosses = [
                item for item in native_features if item.get("kind") == "screw_boss"
            ]
            for ordinal, screw_native in enumerate(screw_bosses, start=1):
                screw_geometry_refs = [
                    GeometryRef.from_dict(item)
                    for item in screw_native.get("geometry_refs", [])
                    if item.get("kind") == "face"
                ]
                if any(
                    ref.topology_snapshot_id != topology_snapshot_id
                    for ref in screw_geometry_refs
                ):
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "OCCT screw-boss refs do not match the topology snapshot.",
                    )
                screw_feature_id = str(screw_native["feature_id"]).replace(
                    "feature-", "feature.", 1
                )
                screw_source_refs = [
                    f"recognizer:{self.key}@{self.version}",
                    f"input:{input_record.input_id}",
                    f"native_feature:{screw_native['feature_id']}",
                ]
                role_face_indices = screw_native.get("parameters", {}).get(
                    "role_face_indices", {}
                )
                if not isinstance(role_face_indices, Mapping):
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "OCCT screw-boss output lacks role_face_indices.",
                    )
                feature_region_refs: list[str] = []
                refs_by_index = {ref.index: ref for ref in screw_geometry_refs}
                for role in (
                    "outer_side",
                    "inner_side",
                    "top",
                    "bottom",
                    "transition",
                    "adjacent_main_wall",
                ):
                    raw_indices = role_face_indices.get(role, [])
                    if not isinstance(raw_indices, list):
                        raise DFMError(
                            "geometry_protocol_invalid",
                            "OCCT screw-boss role faces must be arrays.",
                            {"feature_id": screw_feature_id, "role": role},
                        )
                    role_refs = [
                        refs_by_index[index]
                        for index in raw_indices
                        if isinstance(index, int) and index in refs_by_index
                    ]
                    if not role_refs:
                        continue
                    screw_region_id = (
                        f"region.screw_boss.{suffix}.{ordinal}.{role}"
                    )
                    region_identity = {
                        "region_id": screw_region_id,
                        "input_sha256": input_record.sha256,
                        "mode": "topology_refs",
                        "role": role,
                        "feature_refs": [screw_feature_id],
                        "geometry_refs": [item.to_dict() for item in role_refs],
                    }
                    regions.append(
                        RegionRecord(
                            region_id=screw_region_id,
                            input_sha256=input_record.sha256,
                            coordinate_system="model",
                            mode="topology_refs",
                            semantic_label=f"screw_boss_{role}",
                            source_refs=screw_source_refs,
                            version=self.version,
                            content_sha256=_content_hash(region_identity),
                            role=role,
                            feature_refs=[screw_feature_id],
                            geometry_refs=role_refs,
                        )
                    )
                    feature_region_refs.append(screw_region_id)
                    screw_region_refs.append(screw_region_id)
                if not feature_region_refs:
                    raise DFMError(
                        "geometry_protocol_invalid",
                        "OCCT screw-boss output has no usable semantic regions.",
                        {"feature_id": screw_feature_id},
                    )
                features.append(
                    FeatureRecord(
                        feature_id=screw_feature_id,
                        kind="screw_boss",
                        source_refs=screw_source_refs,
                        confidence=float(screw_native["confidence"]),
                        input_sha256=input_record.sha256,
                        region_refs=feature_region_refs,
                        properties={
                            **screw_native.get("parameters", {}),
                            "method": screw_native.get("method"),
                            "quality": screw_native.get("quality", {}),
                            "diagnostics": screw_native.get("diagnostics", {}),
                        },
                        recognizer=self.key,
                        recognizer_version=self.version,
                        status="detected",
                    )
                )
                screw_feature_refs.append(screw_feature_id)
            recognizers.append(
                RecognizerExecutionResult(
                    recognizer_id="injection-screw-boss",
                    status="completed",
                    implementation_version=f"{self.version}+{ENGINE_VERSION}",
                    feature_refs=screw_feature_refs,
                    region_refs=screw_region_refs,
                    diagnostics={"detected_count": len(screw_feature_refs)},
                )
            )

        geometry_path = output_dir / "geometry_snapshot.step"
        shutil.copyfile(input_path, geometry_path)
        discovery_artifacts = [
            ObjectiveArtifactManifest(
                artifact_id=f"artifact.discovery.geometry.{suffix}",
                kind="geometry_snapshot",
                filename=geometry_path.name,
                media_type="model/step",
                size_bytes=geometry_path.stat().st_size,
                sha256=_sha256(geometry_path),
            ),
            ObjectiveArtifactManifest(
                artifact_id=f"artifact.discovery.topology.{suffix}",
                kind="topology_map",
                filename=topology_path.name,
                media_type="application/json",
                size_bytes=topology_path.stat().st_size,
                sha256=_sha256(topology_path),
            ),
            ObjectiveArtifactManifest(
                artifact_id=f"artifact.discovery.scene.{suffix}",
                kind="render_scene",
                filename=scene_path.name,
                media_type="application/json",
                size_bytes=scene_path.stat().st_size,
                sha256=_sha256(scene_path),
            ),
        ]
        discovery_task = GeometryDiscoveryTaskRequest(
            schema_version=DISCOVERY_SCHEMA_VERSION,
            request_id=f"discovery.request.{suffix}",
            input_id=input_record.input_id,
            input_sha256=input_record.sha256,
            input_format="step",
            process=process,
            recognizer_ids=list(capability["recognizer_ids"]),
            facts={
                name: value
                for name, value in (facts or {}).items()
                if isinstance(value, ResolvedArgument)
            },
        )
        result = GeometryDiscoveryResultManifest(
            schema_version=DISCOVERY_SCHEMA_VERSION,
            producer_version=f"{self.version}+{ENGINE_VERSION}",
            request_id=discovery_task.request_id,
            input_id=input_record.input_id,
            input_sha256=input_record.sha256,
            process=process,
            topology_snapshot_id=topology_snapshot_id,
            render_mesh_snapshot_id=render_mesh_snapshot_id,
            geometry_snapshot_ref=discovery_artifacts[0].artifact_id,
            features=features,
            regions=regions,
            recognizers=recognizers,
            artifacts=discovery_artifacts,
            diagnostics={"objective_run_id": run_id},
        )
        result_path = output_dir / "geometry_discovery_result.json"
        result_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        relative_dir = output_dir.relative_to(project_dir)
        now = _utc_now()
        artifacts = [
            ArtifactRecord(
                artifact_id=item.artifact_id,
                logical_id=item.artifact_id,
                kind=item.kind,
                relative_path=(relative_dir / item.filename).as_posix(),
                media_type=item.media_type,
                created_at=now,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in discovery_artifacts
        ]
        artifacts.append(
            ArtifactRecord(
                artifact_id=f"artifact.discovery.result.{suffix}",
                logical_id=f"artifact.discovery.result.{suffix}",
                kind="geometry_discovery_result",
                relative_path=(relative_dir / result_path.name).as_posix(),
                media_type="application/json",
                created_at=now,
                size_bytes=result_path.stat().st_size,
                sha256=_sha256(result_path),
            )
        )
        return FeatureRecognitionResult(
            features=features,
            regions=regions,
            diagnostics={"task": discovery_task.to_dict(), "result": result.to_dict()},
            artifacts=artifacts,
            topology_snapshot_id=topology_snapshot_id,
            render_mesh_snapshot_id=render_mesh_snapshot_id,
            geometry_snapshot_ref=discovery_artifacts[0].artifact_id,
        )


def build_occt_feature_recognition_provider(
    configured_executable: str = "", *, timeout_seconds: float = 900
) -> OCCTCppFeatureRecognitionProvider:
    return OCCTCppFeatureRecognitionProvider(
        discover_geometry_executable(configured_executable),
        timeout_seconds=timeout_seconds,
    )
