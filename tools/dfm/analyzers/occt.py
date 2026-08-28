"""Native OCCT injection analyzer using the versioned local CLI contract."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Any, Callable

from ..contracts import (
    GEOMETRY_EVENT_CONTRACT,
    GEOMETRY_REQUEST_CONTRACT,
    GEOMETRY_RESULT_CONTRACT,
    OBJECTIVE_SCHEMA_VERSION,
    WORKER_SCHEMA_VERSION,
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    LocalObjectiveWorkerRequest,
    ObjectiveResultManifest,
    ObjectiveTaskRequest,
    WorkerEvent,
)
from ..errors import DFMError
from ..runtime.process import ProcessRunner
from .base import AnalyzerContext, CancellationToken
from .objective_result import validate_objective_result


ENGINE_VERSION = "occt-dfm-geometry-1.4.1"
GEOMETRY_SCOPE_ID = "injection.geometry-core"
GEOMETRY_SCOPE_VERSION = "4.0.0"
CAPABILITY_CONTRACT = "dfm.geometry.capabilities/v1"
GEOMETRY_OPERATION_PAIRS = (
    ("geometry.preflight", "geometry_preflight"),
    ("topology.index", "index_topology"),
    ("topology.aag", "build_aag"),
    ("measure_draft", "measure_draft"),
    ("measure_wall_thickness", "measure_wall_thickness"),
    ("measure_undercut", "measure_undercut"),
    ("measure_sharp_corner", "measure_sharp_corner"),
    ("recognize_drilled_hole", "recognize_drilled_hole"),
    ("recognize_blend", "recognize_blend"),
    ("recognize_shaft", "recognize_shaft"),
    ("recognize_cavity", "recognize_cavity"),
    ("recognize_convex_hull", "recognize_convex_hull"),
    ("recognize_isolated", "recognize_isolated"),
    ("recognize_canonical_surface", "recognize_canonical_surface"),
    ("recognize_surface_probe", "recognize_surface_probe"),
    ("recognize_chamfer", "recognize_chamfer"),
    ("recognize_rib", "recognize_rib"),
)


def _valid_capability_operations(payload: object) -> bool:
    if not isinstance(payload, list) or len(payload) != len(GEOMETRY_OPERATION_PAIRS):
        return False
    try:
        observed = {
            (item["operation_id"], item["calculator_id"])
            for item in payload
            if isinstance(item, dict)
            and item.get("maturity") == "experimental"
            and item.get("algorithm_version") == ENGINE_VERSION
        }
    except (KeyError, TypeError):
        return False
    return observed == set(GEOMETRY_OPERATION_PAIRS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def discover_geometry_executable(configured: str = "") -> str | None:
    """Resolve explicit config, standard repo installs, then PATH.

    Relative configured paths are anchored to the Hermes source root.  The
    desktop backend may be launched with ``apps/desktop`` (or a user-selected
    project) as its working directory, so interpreting config relative to the
    process CWD would make the same ``config.yaml`` behave differently across
    clients.
    """

    root = Path(__file__).resolve().parents[3]

    if configured:
        candidate = Path(configured).expanduser()
        configured_candidates = (
            (candidate,)
            if candidate.is_absolute()
            else (root / candidate, candidate)
        )
        for configured_candidate in configured_candidates:
            if configured_candidate.is_file():
                return str(configured_candidate.resolve())
        discovered = shutil.which(configured)
        return str(Path(discovered).resolve()) if discovered else None

    names = ("dfm-geometry.exe", "dfm-geometry")
    directories = (
        root / "dfm-geometry-exe" / "windows-x64",
        root / "dfm-geometry" / "out" / "install" / "windows-vcpkg-vs2026-sln" / "bin",
        root / "dfm-geometry" / "out" / "install" / "windows-vcpkg-release" / "bin",
        root
        / "dfm-geometry"
        / "out"
        / "install"
        / "windows-vcpkg-vs2026-ninja-release"
        / "bin",
        root
        / "dfm-geometry"
        / "out"
        / "install"
        / "windows-external-occt-release"
        / "bin",
        root / "dfm-geometry" / "out" / "build" / "windows-vcpkg-release" / "Release",
        root / "dfm-geometry" / "out" / "build" / "windows-vcpkg-vs2026-ninja-release",
        root
        / "dfm-geometry"
        / "out"
        / "build"
        / "windows-external-occt-release"
        / "Release",
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
    try:
        completed = subprocess.run(
            [executable, "capabilities"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DFMError(
            "geometry_engine_unhealthy",
            "The configured DFM geometry executable could not be queried.",
            {"executable": executable},
        ) from exc
    if completed.returncode != 0:
        raise DFMError(
            "geometry_engine_unhealthy",
            "The DFM geometry executable rejected its capability query.",
            {"returncode": completed.returncode},
        )
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DFMError(
            "geometry_protocol_invalid",
            "The DFM geometry executable returned invalid capability JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            "geometry_protocol_invalid",
            "The DFM geometry capability response must be an object.",
        )
    return payload


class OcctAnalyzer:
    key = "occt_cpp"
    version = ENGINE_VERSION
    supported_inputs = ("step",)

    def __init__(
        self,
        executable: str | None = None,
        *,
        runner: ProcessRunner | None = None,
        capability_probe: Callable[[str], dict[str, Any]] | None = None,
        timeout_seconds: float = 900,
    ) -> None:
        self.executable = executable
        self.runner = runner or ProcessRunner()
        self.capability_probe = capability_probe or probe_geometry_executable
        self.timeout_seconds = timeout_seconds
        self._capability_payload: dict[str, Any] | None = None
        self._capability_error: DFMError | None = None
        self._capability_lock = threading.Lock()

    def _probe(self) -> dict[str, Any] | None:
        if self.executable is None:
            return None
        if self._capability_payload is None and self._capability_error is None:
            with self._capability_lock:
                if self._capability_payload is None and self._capability_error is None:
                    try:
                        payload = self.capability_probe(self.executable)
                        if (
                            payload.get("contract_version") != CAPABILITY_CONTRACT
                            or payload.get("engine_version") != self.version
                            or payload.get("backend") != "analysis_situs+occt"
                            or payload.get("analysis_situs_version") != "v2025.2"
                            or payload.get("analysis_situs_commit")
                            != "aa5958932c8c85c068566ab685f2b99c0436b926"
                            or payload.get("status") != "available"
                            or payload.get("maturity") != "experimental"
                            or payload.get("objective_schema_version")
                            != OBJECTIVE_SCHEMA_VERSION
                            or payload.get("supported_processes") != ["injection"]
                            or payload.get("supported_formats") != ["step"]
                            or payload.get("supported_extensions") != [".step", ".stp"]
                            or payload.get("output_artifact_kinds")
                            != [
                                "preflight",
                                "topology_map",
                                "render_scene",
                                "features",
                                "measurements",
                                "scalar_field",
                            ]
                            or not _valid_capability_operations(
                                payload.get("operations")
                            )
                        ):
                            raise DFMError(
                                "geometry_protocol_invalid",
                                "The DFM geometry capability contract is incompatible.",
                            )
                        self._capability_payload = payload
                    except DFMError as exc:
                        self._capability_error = exc
        return self._capability_payload

    def capability(self, context: AnalyzerContext) -> Capability:
        if self.executable is None:
            return Capability(
                self.key,
                CapabilityStatus.DEPENDENCY_MISSING,
                "The Analysis Situs/OCCT DFM geometry executable was not found.",
                "geometry_engine_missing",
                {
                    "config": "dfm.geometry.executable",
                    "expected_engine_version": self.version,
                    "supported_formats": ["step"],
                },
            )
        payload = self._probe()
        if self._capability_error is not None:
            return Capability(
                self.key,
                CapabilityStatus.UNHEALTHY,
                self._capability_error.message,
                self._capability_error.code,
                self._capability_error.details,
            )
        if payload is None:
            return Capability(
                self.key,
                CapabilityStatus.UNHEALTHY,
                "The Analysis Situs/OCCT DFM geometry capability is unavailable.",
                "geometry_engine_unhealthy",
            )
        if context.plan is not None and context.plan.process != "injection":
            return Capability(
                self.key,
                CapabilityStatus.NOT_IMPLEMENTED,
                "The initial OCCT geometry engine supports injection analysis only.",
                "unsupported_capability",
                {
                    "requested_process": context.plan.process,
                    "supported_processes": ["injection"],
                },
            )
        if context.plan is not None and getattr(
            context.plan, "verification_level", "experimental"
        ) != "experimental":
            return Capability(
                self.key,
                CapabilityStatus.DISABLED,
                "Analysis Situs/OCCT DFM algorithms are experimental and require explicit opt-in.",
                "verification_unavailable",
                {
                    "requested": context.plan.verification_level,
                    "available": ["experimental"],
                },
            )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "The Analysis Situs/OCCT injection geometry engine is available.",
            details={
                "engine_version": self.version,
                "occt_version": payload.get("occt_version"),
                "analysis_situs_version": payload.get("analysis_situs_version"),
                "analysis_situs_commit": payload.get("analysis_situs_commit"),
                "maturity": "experimental",
                "supported_processes": ["injection"],
                "format_ids": ["step"],
                "operations": payload.get("operations", []),
            },
        )

    def run(
        self,
        context: AnalyzerContext,
        cancellation: CancellationToken,
    ) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        capability = self.capability(context)
        if capability.status is not CapabilityStatus.AVAILABLE:
            raise DFMError(
                capability.error_code or capability.status.value,
                capability.reason,
                capability.details,
            )
        if context.plan is None:
            raise DFMError(
                "plan_required", "A persisted DFM execution plan is required."
            )
        if context.plan.process != "injection":
            raise DFMError(
                "unsupported_capability",
                "The initial OCCT geometry engine supports injection analysis only.",
                {"supported_processes": ["injection"]},
            )
        input_record = next(
            (
                item
                for item in context.inputs
                if item.input_id in context.plan.input_ids and item.kind == "step"
            ),
            None,
        )
        if input_record is None:
            raise DFMError(
                "input_required", "The OCCT plan does not reference a STEP input."
            )

        run_dir = context.project_dir / "runs" / context.run_id
        output_dir = run_dir / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        task = ObjectiveTaskRequest(
            schema_version=OBJECTIVE_SCHEMA_VERSION,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            input_format="step",
            process=context.plan.process,
            # Plan scope identifies the pinned ontology/rule publication. The
            # external engine has its own capability scope and must never be
            # handed Hermes semantic scope identities as geometry policy.
            scope_id=GEOMETRY_SCOPE_ID,
            scope_version=GEOMETRY_SCOPE_VERSION,
            operations=context.plan.operations,
            regions=context.plan.regions,
        )
        request = LocalObjectiveWorkerRequest(
            schema_version=WORKER_SCHEMA_VERSION,
            backend_version=self.version,
            input_path=str(
                (context.project_dir / input_record.relative_path).resolve()
            ),
            output_dir=str(output_dir.resolve()),
            task=task,
            contract_version=GEOMETRY_REQUEST_CONTRACT,
        )
        request_path = run_dir / "request.json"
        request_path.write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        events: list[WorkerEvent] = []

        def handle_event(event: WorkerEvent) -> None:
            if event.contract_version != GEOMETRY_EVENT_CONTRACT:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The OCCT engine emitted an event with an incompatible contract.",
                    {"contract_version": event.contract_version},
                )
            events.append(event)
            if context.event_sink is not None:
                context.event_sink(event)

        assert self.executable is not None
        process_result = self.runner.run(
            [self.executable, "analyze", "--request", str(request_path)],
            Path(__file__).resolve().parents[3],
            self.timeout_seconds,
            cancellation,
            handle_event,
            run_dir / "worker.stdout.log",
            run_dir / "worker.stderr.log",
        )
        self._validate_jsonl_stdout(process_result.stdout, events)
        error_events = [event for event in events if event.type == "error"]
        if process_result.returncode != 0:
            if (
                len(error_events) != 1
                or any(event.type == "completed" for event in events)
                or not events
                or events[-1] != error_events[0]
            ):
                raise DFMError(
                    "geometry_protocol_invalid",
                    "A failed OCCT process must end with exactly one error event.",
                )
            error = error_events[0]
            raise DFMError(
                error.code if error and error.code else "objective_backend_failed",
                error.message
                if error and error.message
                else "The OCCT geometry engine failed.",
            )
        if error_events:
            raise DFMError(
                "geometry_protocol_invalid",
                "A successful OCCT process emitted an error event.",
            )
        completed = [event for event in events if event.type == "completed"]
        if (
            len(completed) != 1
            or completed[0].path != "engine_result.json"
            or not events
            or events[-1] != completed[0]
        ):
            raise DFMError(
                "objective_result_invalid",
                "The OCCT engine did not emit exactly one completion result.",
            )
        artifact_events = [event for event in events if event.type == "artifact"]
        artifact_pairs = [(event.kind, event.path) for event in artifact_events]
        if len(artifact_pairs) != len(set(artifact_pairs)):
            raise DFMError(
                "geometry_protocol_invalid",
                "The OCCT engine emitted duplicate artifact events.",
            )
        result_path = self._contained_file(output_dir, completed[0].path)
        try:
            result = ObjectiveResultManifest.from_dict(
                json.loads(result_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DFMError(
                "objective_result_invalid",
                "The OCCT engine result could not be loaded.",
            ) from exc
        if (
            result.schema_version != OBJECTIVE_SCHEMA_VERSION
            or result.contract_version != GEOMETRY_RESULT_CONTRACT
            or result.producer_version != self.version
            or result.run_id != context.run_id
            or result.input_sha256 != input_record.sha256
            or result.process != context.plan.process
            or result.scope_id != GEOMETRY_SCOPE_ID
            or result.scope_version != GEOMETRY_SCOPE_VERSION
            or result.result_path != "engine_result.json"
        ):
            raise DFMError(
                "objective_result_invalid",
                "The OCCT engine result does not match its persisted plan.",
            )
        required_kinds = {
            "preflight",
            "topology_map",
            "render_scene",
            "features",
            "measurements",
        }
        allowed_kinds = required_kinds | {"scalar_field"}
        artifact_kinds = [item.kind for item in result.artifacts]
        kind_counts = {kind: artifact_kinds.count(kind) for kind in allowed_kinds}
        requires_scalar_field = any(
            "scalar_field" in operation.required_artifacts
            for operation in context.plan.operations
        )
        if (
            any(kind_counts[kind] != 1 for kind in required_kinds)
            or set(artifact_kinds) - allowed_kinds
            or (requires_scalar_field and kind_counts["scalar_field"] == 0)
        ):
            raise DFMError(
                "objective_result_invalid",
                "The OCCT engine result has an incomplete or unexpected artifact set.",
            )
        expected_artifact_pairs = {
            (item.kind, item.filename) for item in result.artifacts
        } | {("worker_result", "engine_result.json")}
        if set(artifact_pairs) != expected_artifact_pairs:
            raise DFMError(
                "geometry_protocol_invalid",
                "The OCCT artifact events do not match the signed result manifest.",
            )

        artifacts: list[ArtifactRecord] = []
        documents: dict[str, tuple[ArtifactRecord, dict[str, Any]]] = {}
        for item in result.artifacts:
            path = self._contained_file(output_dir, item.filename)
            if (
                path.stat().st_size != item.size_bytes
                or self._sha256(path) != item.sha256
            ):
                raise DFMError(
                    "objective_artifact_invalid",
                    "An OCCT artifact does not match its manifest.",
                    {"artifact_id": item.artifact_id},
                )
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DFMError(
                    "objective_artifact_invalid",
                    "An OCCT artifact is not valid UTF-8 JSON.",
                    {"artifact_id": item.artifact_id},
                ) from exc
            if not isinstance(document, dict):
                raise DFMError(
                    "objective_artifact_invalid",
                    "An OCCT artifact must contain a JSON object.",
                    {"artifact_id": item.artifact_id},
                )
            record = ArtifactRecord(
                item.artifact_id,
                item.kind,
                path.relative_to(context.project_dir).as_posix(),
                item.media_type,
                _utc_now(),
                run_id=context.run_id,
                logical_id=item.artifact_id,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            documents[item.artifact_id] = (record, document)
            artifacts.append(record)
        self._validate_artifact_contracts(
            documents,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            process=context.plan.process,
            scope_id=GEOMETRY_SCOPE_ID,
            scope_version=GEOMETRY_SCOPE_VERSION,
        )
        validate_objective_result(
            context.plan.operations,
            context.project_dir,
            artifacts,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            process=context.plan.process,
            scope_id=GEOMETRY_SCOPE_ID,
            regions=context.plan.regions,
        )
        artifacts.append(
            self._artifact_record(
                context.project_dir,
                context.run_id,
                "engine_result",
                "worker_result",
                result_path,
            )
        )
        return artifacts

    @classmethod
    def _validate_artifact_contracts(
        cls,
        documents: dict[str, tuple[ArtifactRecord, dict[str, Any]]],
        *,
        run_id: str,
        input_sha256: str,
        process: str,
        scope_id: str,
        scope_version: str,
    ) -> None:
        by_kind: dict[str, list[tuple[ArtifactRecord, dict[str, Any]]]] = {}
        for record, document in documents.values():
            by_kind.setdefault(record.kind, []).append((record, document))
        for kind in (
            "preflight",
            "topology_map",
            "render_scene",
            "features",
            "measurements",
        ):
            if len(by_kind.get(kind, [])) != 1:
                raise DFMError(
                    "objective_result_invalid",
                    "OCCT artifact documents are incomplete.",
                    {"kind": kind},
                )

        preflight = by_kind["preflight"][0][1]
        features_document = by_kind["features"][0][1]
        measurements_document = by_kind["measurements"][0][1]
        native_contracts = {
            "preflight": "dfm.geometry.artifact/preflight/v1",
            "features": "dfm.geometry.artifact/features/v1",
            "measurements": "dfm.geometry.artifact/measurements/v1",
        }
        for kind, contract in native_contracts.items():
            document = by_kind[kind][0][1]
            if (
                document.get("schema_version") != 1
                or document.get("contract_version") != contract
                or document.get("run_id") != run_id
                or document.get("input_sha256") != input_sha256
            ):
                raise DFMError(
                    "objective_result_invalid",
                    "An OCCT native artifact has an incompatible identity.",
                    {"kind": kind},
                )

        healed = preflight.get("healed")
        if (
            preflight.get("engine_version") != ENGINE_VERSION
            or preflight.get("format") != "step"
            or preflight.get("unit") != "mm"
            or not isinstance(healed, bool)
            or preflight.get("status") != "passed"
            or preflight.get("valid_brep") is not True
        ):
            raise DFMError(
                "objective_result_invalid",
                "OCCT preflight identity is invalid.",
            )

        preflight_diagnostics = preflight.get("diagnostics")
        if not isinstance(preflight_diagnostics, dict):
            raise DFMError(
                "objective_result_invalid",
                "OCCT geometry normalization audit is missing.",
            )
        if healed:
            operations = preflight_diagnostics.get("shape_process_operations")
            strict_validation = preflight_diagnostics.get("strict_validation")
            processed_validation = preflight_diagnostics.get(
                "post_shape_process_validation"
            )
            if (
                preflight_diagnostics.get("step_shape_processing_disabled") is not False
                or preflight_diagnostics.get("shape_process_attempted") is not True
                or preflight_diagnostics.get("geometry_healing_applied") is not True
                or preflight_diagnostics.get("geometry_healing_succeeded") is not True
                or preflight_diagnostics.get("selected_transfer") != "shape_processed"
                or not isinstance(operations, list)
                or "FixShape" not in operations
                or not isinstance(strict_validation, dict)
                or strict_validation.get("analyzable") is not False
                or not isinstance(processed_validation, dict)
                or processed_validation.get("analyzable") is not True
                or processed_validation.get("valid_brep") is not True
                or processed_validation.get("bbox_status") != "finite"
            ):
                raise DFMError(
                    "objective_result_invalid",
                    "OCCT FixShape normalization audit is inconsistent.",
                )
        elif (
            preflight_diagnostics.get("shape_process_attempted") is True
            or preflight_diagnostics.get("geometry_healing_applied") is True
            or preflight_diagnostics.get("geometry_healing_succeeded") is True
            or preflight_diagnostics.get("selected_transfer") == "shape_processed"
        ):
            raise DFMError(
                "objective_result_invalid",
                "OCCT artifacts claim unaudited geometry normalization.",
            )
        for kind, document in (
            ("features", features_document),
            ("measurements", measurements_document),
        ):
            if (
                document.get("process") != process
                or document.get("scope_id") != scope_id
                or document.get("scope_version") != scope_version
            ):
                raise DFMError(
                    "objective_result_invalid",
                    "An OCCT objective artifact has an incompatible scope.",
                    {"kind": kind},
                )

        features = features_document.get("features")
        measurements = measurements_document.get("measurements")
        if not isinstance(features, list) or not isinstance(measurements, list):
            raise DFMError(
                "objective_result_invalid",
                "OCCT feature and measurement collections must be arrays.",
            )
        records = [*features, *measurements]
        scalar_fields = [document for _, document in by_kind.get("scalar_field", [])]
        quality_records = [preflight, *records, *scalar_fields]
        if any(
            not isinstance(record, dict)
            or not isinstance(record.get("quality"), dict)
            or record["quality"].get("backend")
            not in {"occt", "analysis_situs+occt"}
            or record["quality"].get("maturity") != "experimental"
            or record["quality"].get("certified") is not False
            for record in quality_records
        ):
            raise DFMError(
                "objective_result_invalid",
                "OCCT artifact quality metadata is invalid.",
            )
        if any(
            not isinstance(record.get("diagnostics"), dict)
            for record in [preflight, *records]
        ):
            raise DFMError(
                "objective_result_invalid",
                "OCCT native artifact diagnostics are invalid.",
            )
        if any(
            record.get("algorithm_version") != ENGINE_VERSION
            or record.get("input_sha256") != input_sha256
            or not isinstance(record.get("method"), str)
            or not record["method"]
            for record in records
        ):
            raise DFMError(
                "objective_result_invalid",
                "OCCT feature/measurement algorithm identity is invalid.",
            )

        for record, document in documents.values():
            identity_key = {
                "topology_map": "map_id",
                "render_scene": "scene_id",
                "scalar_field": "field_id",
            }.get(record.kind)
            if identity_key is not None and document.get(identity_key) != record.artifact_id:
                raise DFMError(
                    "objective_result_invalid",
                    "A shared OCCT artifact ID does not match its payload identity.",
                    {"kind": record.kind, "artifact_id": record.artifact_id},
                )

        topology = by_kind["topology_map"][0][1]
        topology_snapshot_id = str(
            topology.get("topology_snapshot", {}).get("topology_snapshot_id") or ""
        )
        topology_entities = {
            (
                item.get("geometry_ref", {}).get("kind"),
                item.get("geometry_ref", {}).get("index"),
                item.get("geometry_ref", {}).get("entity_id"),
            )
            for item in topology.get("faces", [])
            if isinstance(item, dict)
        }
        for record in records:
            refs = record.get("geometry_refs")
            if not isinstance(refs, list) or any(
                not isinstance(ref, dict)
                or ref.get("input_sha256") != input_sha256
                or ref.get("topology_snapshot_id") != topology_snapshot_id
                or not ref.get("entity_id")
                or ref.get("kind") not in {"face", "edge"}
                or (
                    ref.get("kind") == "face"
                    and (ref.get("kind"), ref.get("index"), ref.get("entity_id"))
                    not in topology_entities
                )
                for ref in refs
            ):
                raise DFMError(
                    "objective_result_invalid",
                    "An OCCT geometry reference does not resolve in the shared topology snapshot.",
                )

    @staticmethod
    def _validate_jsonl_stdout(raw_stdout: str, observed: list[WorkerEvent]) -> None:
        """Require every native stdout line to be one geometry event JSON object."""

        if not raw_stdout:
            # Injected runners used by unit tests may deliver events directly.
            # A real ProcessRunner always returns the same stdout it parsed.
            return
        parsed: list[WorkerEvent] = []
        for line in raw_stdout.splitlines():
            if not line.strip():
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The OCCT engine emitted a blank stdout line.",
                )
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise TypeError("event must be an object")
                event = WorkerEvent.from_dict(payload)
            except (json.JSONDecodeError, TypeError, ValueError, DFMError) as exc:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The OCCT engine stdout is not pure geometry event JSONL.",
                ) from exc
            if event.contract_version != GEOMETRY_EVENT_CONTRACT:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The OCCT engine stdout contains an incompatible event contract.",
                )
            common = {"schema_version", "contract_version", "type"}
            fields_by_type = {
                "progress": {"stage", "percent"},
                "artifact": {"kind", "path"},
                "completed": {"path"},
                "error": {"code", "message"},
            }
            if set(payload) != common | fields_by_type[event.type]:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The OCCT engine event fields do not match their event type.",
                )
            valid_values = (
                type(payload["schema_version"]) is int
                and payload["schema_version"] == WORKER_SCHEMA_VERSION
                and (
                    event.type == "progress"
                    and isinstance(event.stage, str)
                    and bool(event.stage)
                    and type(event.percent) is int
                    or event.type == "artifact"
                    and isinstance(event.kind, str)
                    and bool(event.kind)
                    and isinstance(event.path, str)
                    and bool(event.path)
                    or event.type == "completed"
                    and isinstance(event.path, str)
                    and bool(event.path)
                    or event.type == "error"
                    and isinstance(event.code, str)
                    and bool(event.code)
                    and isinstance(event.message, str)
                    and bool(event.message)
                )
            )
            if not valid_values:
                raise DFMError(
                    "geometry_protocol_invalid",
                    "The OCCT engine event values are invalid for their event type.",
                )
            parsed.append(event)
        if parsed != observed:
            raise DFMError(
                "geometry_protocol_invalid",
                "The OCCT engine stdout event stream was not consumed deterministically.",
            )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _artifact_record(
        cls,
        project_dir: Path,
        run_id: str,
        artifact_id: str,
        kind: str,
        path: Path,
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id,
            kind,
            path.relative_to(project_dir).as_posix(),
            "application/json",
            _utc_now(),
            run_id=run_id,
            logical_id=artifact_id,
            size_bytes=path.stat().st_size,
            sha256=cls._sha256(path),
        )

    @staticmethod
    def _contained_file(output_dir: Path, raw_path: str) -> Path:
        relative = Path(raw_path)
        resolved = (output_dir / relative).resolve()
        if (
            not raw_path
            or relative.is_absolute()
            or not resolved.is_relative_to(output_dir.resolve())
            or not resolved.is_file()
        ):
            raise DFMError(
                "objective_artifact_invalid",
                "The OCCT engine returned an invalid artifact path.",
                {"path": raw_path},
            )
        return resolved
