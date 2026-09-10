"""Parasolid analysis through the configured remote Siemens NX service."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import time
from pathlib import Path

from ..backends.nx.client import NXBackendClient
from ..backends.nx.contracts import NX_REQUEST_SCHEMA_VERSION
from ..contracts import (
    OBJECTIVE_SCHEMA_VERSION,
    ArtifactRecord,
    Capability,
    CapabilityStatus,
    ObjectiveTaskRequest,
    STAGE_OBJECTIVE_READY,
    WorkerEvent,
    normalize_objective_error,
    normalize_objective_stage,
)
from ..errors import DFMError
from .base import AnalyzerContext, CancellationToken
from .objective_result import validate_objective_result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ParasolidAnalyzer:
    key = "parasolid"
    version = "nx-http-v4"
    supported_inputs = ("parasolid", "geometry", "fusion")

    def __init__(
        self,
        client: NXBackendClient | None = None,
        *,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.poll_interval_seconds = poll_interval_seconds

    def capability(self, context: AnalyzerContext) -> Capability:
        if self.client is None:
            return Capability(
                self.key,
                CapabilityStatus.DEPENDENCY_MISSING,
                "NX HTTP backend is not configured.",
                "dependency_missing",
                {"config": "dfm.nx.endpoint", "transport": "http"},
            )
        try:
            remote = self.client.capability()
        except DFMError as exc:
            return Capability(
                self.key,
                CapabilityStatus.UNHEALTHY,
                exc.message,
                exc.code,
                exc.details or {},
            )
        format_status = remote.formats.get("parasolid_xt", "not_implemented")
        if remote.status != "available" or format_status != "available":
            return Capability(
                self.key,
                CapabilityStatus.NOT_IMPLEMENTED
                if format_status == "not_implemented"
                else CapabilityStatus.UNHEALTHY,
                "NX backend does not currently provide certified Parasolid XT loading.",
                "unsupported_capability",
                {
                    "remote_status": remote.status,
                    "format_status": format_status,
                    "backend_version": remote.backend_version,
                },
            )
        if context.plan is not None:
            required = [
                item
                for item in context.plan.operations
                if item.calculator_id not in {"load_geometry", "render_evidence"}
            ]
            uncertified = sorted({
                operation.calculator_id
                for operation in required
                if remote.calculator(operation.calculator_id).status != "certified"
            })
            if uncertified:
                return Capability(
                    self.key,
                    CapabilityStatus.NOT_IMPLEMENTED,
                    "NX backend does not certify every calculator required by this plan.",
                    "unsupported_capability",
                    {
                        "uncertified_operations": uncertified,
                        "calculator_statuses": {
                            key: value.to_dict()
                            for key, value in remote.calculators.items()
                        },
                    },
                )
            incompatible = []
            for operation in required:
                calculator_id = operation.calculator_id
                definition = remote.calculator(calculator_id)
                supplied = set(operation.arguments)
                supplied_algorithm_options = set(operation.algorithm_options)
                required_arguments = set(definition.required_arguments)
                supported_arguments = required_arguments | set(
                    definition.optional_arguments
                )
                reasons = []
                if definition.contract_version != NX_REQUEST_SCHEMA_VERSION:
                    reasons.append("contract_version")
                if not required_arguments.issubset(supplied):
                    reasons.append("required_arguments")
                if supplied - supported_arguments:
                    reasons.append("unsupported_arguments")
                if supplied_algorithm_options - set(
                    definition.supported_algorithm_options
                ):
                    reasons.append("unsupported_algorithm_options")
                if set(operation.required_quantities) - set(
                    definition.output_quantities
                ):
                    reasons.append("output_quantities")
                if set(operation.required_artifacts) - set(
                    definition.output_artifact_kinds
                ):
                    reasons.append("output_artifacts")
                if definition.supported_formats and "parasolid_xt" not in set(
                    definition.supported_formats
                ):
                    reasons.append("format")
                if operation.region_refs:
                    region_by_id = {
                        item.region_id: item for item in context.plan.regions
                    }
                    resolved_regions = [
                        region_by_id.get(ref) for ref in operation.region_refs
                    ]
                    if any(item is None for item in resolved_regions):
                        reasons.append("region_unresolved")
                    elif not definition.supported_region_modes:
                        reasons.append("region_mode")
                    elif any(
                        item.mode not in definition.supported_region_modes
                        for item in resolved_regions
                        if item is not None
                    ):
                        reasons.append("region_mode")
                if reasons:
                    incompatible.append({
                        "operation_id": operation.operation_id,
                        "calculator_id": calculator_id,
                        "reasons": reasons,
                    })
            if incompatible:
                return Capability(
                    self.key,
                    CapabilityStatus.NOT_IMPLEMENTED,
                    "NX backend does not certify the task contract required by this plan.",
                    "unsupported_capability",
                    {"incompatible_operation_contracts": incompatible},
                )
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "The remote NX Parasolid analyzer is available.",
            details={
                "transport": "http",
                "backend_version": remote.backend_version,
                "plugin_version": remote.plugin_version,
                "calculators": {
                    key: value.to_dict() for key, value in remote.calculators.items()
                },
                "format_id": "parasolid_xt",
                "representation": "brep",
            },
        )

    def run(
        self, context: AnalyzerContext, cancellation: CancellationToken
    ) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        if self.client is None or context.plan is None:
            raise DFMError(
                "plan_required",
                "A configured NX backend and persisted plan are required.",
            )
        capability = self.capability(context)
        if capability.status is not CapabilityStatus.AVAILABLE:
            raise DFMError(
                capability.error_code or capability.status.value,
                capability.reason,
                capability.details,
            )
        input_record = next(
            (
                item
                for item in context.inputs
                if item.input_id in context.plan.input_ids
                and item.format_id == "parasolid_xt"
            ),
            None,
        )
        if input_record is None:
            raise DFMError(
                "input_required",
                "The DFM plan does not reference a Parasolid x_t input.",
            )
        input_path = (context.project_dir / input_record.relative_path).resolve()
        request = ObjectiveTaskRequest(
            schema_version=OBJECTIVE_SCHEMA_VERSION,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            input_format=input_record.format_id,
            process=context.plan.process,
            scope_id=context.plan.scope_id,
            scope_version=context.plan.scope_version,
            operations=context.plan.operations,
            regions=[
                item
                for item in context.plan.regions
                if item.input_sha256 == input_record.sha256
                and any(
                    item.region_id in operation.region_refs
                    for operation in context.plan.operations
                )
            ],
        )
        job = self.client.submit(request.to_dict(), input_path)
        if not job.job_id:
            raise DFMError("nx_protocol_invalid", "NX backend did not return a job_id.")
        self._emit(context, job.stage, job.progress_percent, job.job_id)
        while job.status not in {"succeeded", "failed", "cancelled"}:
            if cancellation.is_cancelled:
                self.client.cancel(job.job_id)
                cancellation.raise_if_cancelled()
            self._emit(
                context, job.stage, job.progress_percent, job.job_id
            )
            time.sleep(self.poll_interval_seconds)
            job = self.client.status(job.job_id)
        if job.status != "succeeded":
            error = job.error or {}
            raise DFMError(
                normalize_objective_error(str(error.get("code") or "")),
                str(error.get("message") or f"NX job ended with status {job.status}."),
                {"nx_job_id": job.job_id},
            )

        output_dir = context.project_dir / "runs" / context.run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[ArtifactRecord] = []
        artifact_ids: set[str] = set()
        filenames: set[str] = set()
        result = self.client.result(job.job_id)
        if (
            result.schema_version != OBJECTIVE_SCHEMA_VERSION
            or result.run_id != context.run_id
            or result.input_sha256 != input_record.sha256
            or result.process != context.plan.process
            or result.scope_id != context.plan.scope_id
            or result.scope_version != context.plan.scope_version
        ):
            raise DFMError(
                "objective_result_invalid",
                "NX objective result does not match the submitted task identity.",
            )
        for remote in result.artifacts:
            filename = Path(remote.filename).name
            if (
                not filename
                or filename != remote.filename
                or filename in filenames
                or remote.size_bytes < 0
                or not re.fullmatch(r"[A-Za-z0-9._-]+", remote.artifact_id)
                or remote.artifact_id in artifact_ids
            ):
                raise DFMError(
                    "objective_artifact_invalid",
                    "NX backend returned an unsafe or duplicate artifact identity.",
                )
            filenames.add(filename)
            artifact_ids.add(remote.artifact_id)
            target = output_dir / filename
            temporary = output_dir / f".{filename}.part"
            try:
                with temporary.open("wb") as handle:
                    self.client.download(job.job_id, remote, handle)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            artifacts.append(
                ArtifactRecord(
                    remote.artifact_id,
                    remote.kind,
                    target.relative_to(context.project_dir).as_posix(),
                    remote.media_type,
                    _utc_now(),
                    run_id=context.run_id,
                    logical_id=remote.artifact_id,
                    size_bytes=remote.size_bytes,
                    sha256=remote.sha256,
                )
            )
        manifest_path = output_dir / "objective_result_manifest.json"
        manifest_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest_content = manifest_path.read_bytes()
        artifacts.append(
            ArtifactRecord(
                "objective_result_manifest",
                "worker_result",
                manifest_path.relative_to(context.project_dir).as_posix(),
                "application/json",
                _utc_now(),
                run_id=context.run_id,
                logical_id="objective_result_manifest",
                size_bytes=len(manifest_content),
                sha256=hashlib.sha256(manifest_content).hexdigest(),
            )
        )
        if not any(item.kind == "measurements" for item in artifacts):
            raise DFMError(
                "objective_result_invalid",
                "NX backend result must include a measurements artifact.",
            )
        validate_objective_result(
            context.plan.operations,
            context.project_dir,
            artifacts,
            run_id=context.run_id,
            input_sha256=input_record.sha256,
            process=context.plan.process,
            scope_id=context.plan.scope_id,
            regions=context.plan.regions,
            error_code="objective_result_invalid",
        )
        self._emit(context, STAGE_OBJECTIVE_READY, 100, job.job_id)
        return artifacts

    @staticmethod
    def _emit(
        context: AnalyzerContext, stage: str, percent: int, job_id: str = ""
    ) -> None:
        if context.event_sink is not None:
            context.event_sink(
                WorkerEvent(
                    1,
                    "progress",
                    stage=normalize_objective_stage(stage),
                    percent=max(5, min(70, round(int(percent) * 0.7))),
                    external_job_id=job_id or None,
                )
            )
