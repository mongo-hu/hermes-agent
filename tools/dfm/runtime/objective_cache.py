"""Content-addressed reuse for backend-neutral objective operations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from ..contracts import ArtifactRecord, OBJECTIVE_SCHEMA_VERSION, PlanRecord
from ..errors import DFMError
from ..analyzers.objective_result import validate_objective_result


_OBJECTIVE_KINDS = {"measurements", "scalar_field", "render_scene", "topology_map"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def operation_fingerprints(
    plan: PlanRecord,
    *,
    input_sha256: str,
    analyzer_key: str,
    analyzer_version: str,
) -> dict[str, str]:
    """Fingerprint every operation with its resolved dependency inputs."""

    fingerprints: dict[str, str] = {}
    for operation in plan.operations:
        fingerprints[operation.operation_id] = _digest(
            {
                "objective_schema_version": OBJECTIVE_SCHEMA_VERSION,
                "analyzer_key": analyzer_key,
                "analyzer_version": analyzer_version,
                "input_sha256": input_sha256,
                "process": plan.process,
                "operation": operation.to_dict(),
                "dependencies": {
                    dependency: fingerprints[dependency]
                    for dependency in operation.depends_on
                },
            }
        )
    return fingerprints


class ObjectiveOperationCache:
    """Publish and compose checkpoints for individual objective operations."""

    @staticmethod
    def _manifest_path(project_dir: Path, fingerprint: str) -> Path:
        return project_dir / "artifacts" / "objective-cache" / f"{fingerprint}.json"

    def publish(
        self,
        project_dir: Path,
        run_id: str,
        plan: PlanRecord,
        artifacts: list[ArtifactRecord],
        *,
        input_sha256: str,
        analyzer_key: str,
        analyzer_version: str,
    ) -> None:
        selected = [item for item in artifacts if item.kind in _OBJECTIVE_KINDS]
        if not selected or not any(item.kind == "measurements" for item in selected):
            return
        fingerprints = operation_fingerprints(
            plan,
            input_sha256=input_sha256,
            analyzer_key=analyzer_key,
            analyzer_version=analyzer_version,
        )
        scalar_operations = {}
        for artifact in selected:
            if artifact.kind != "scalar_field":
                continue
            try:
                scalar_operations[artifact.artifact_id] = str(
                    json.loads(
                        (project_dir / artifact.relative_path).read_text(
                            encoding="utf-8"
                        )
                    ).get("operation_id")
                    or ""
                )
            except (OSError, ValueError, AttributeError):
                return
        for operation in plan.operations:
            if not operation.metric_ids:
                continue
            fingerprint = fingerprints[operation.operation_id]
            operation_artifacts = [
                item
                for item in selected
                if item.kind != "scalar_field"
                or scalar_operations.get(item.artifact_id) == operation.operation_id
            ]
            path = self._manifest_path(project_dir, fingerprint)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "objective_schema_version": OBJECTIVE_SCHEMA_VERSION,
                "operation_id": operation.operation_id,
                "operation_fingerprint": fingerprint,
                "source_run_id": run_id,
                "input_sha256": input_sha256,
                "analyzer_key": analyzer_key,
                "analyzer_version": analyzer_version,
                "artifacts": [item.to_dict() for item in operation_artifacts],
            }
            temporary = path.parent / f".{run_id}.{fingerprint[:8]}.tmp"
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)

    def restore(
        self,
        project_dir: Path,
        run_id: str,
        plan: PlanRecord,
        *,
        input_sha256: str,
        analyzer_key: str,
        analyzer_version: str,
    ) -> list[ArtifactRecord] | None:
        fingerprints = operation_fingerprints(
            plan,
            input_sha256=input_sha256,
            analyzer_key=analyzer_key,
            analyzer_version=analyzer_version,
        )
        requested = [item for item in plan.operations if item.metric_ids]
        if not requested:
            return None
        records_by_id: dict[str, ArtifactRecord] = {}
        measurement_sources: dict[str, ArtifactRecord] = {}
        for operation in requested:
            fingerprint = fingerprints[operation.operation_id]
            path = self._manifest_path(project_dir, fingerprint)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records = [
                    ArtifactRecord.from_dict(item)
                    for item in payload.get("artifacts", [])
                ]
            except (OSError, TypeError, ValueError):
                return None
            if (
                not isinstance(payload, dict)
                or payload.get("objective_schema_version")
                != OBJECTIVE_SCHEMA_VERSION
                or payload.get("operation_id") != operation.operation_id
                or payload.get("operation_fingerprint") != fingerprint
                or payload.get("input_sha256") != input_sha256
                or payload.get("analyzer_key") != analyzer_key
                or payload.get("analyzer_version") != analyzer_version
            ):
                return None
            measurement = next(
                (item for item in records if item.kind == "measurements"), None
            )
            if measurement is None:
                return None
            measurement_sources[operation.operation_id] = measurement
            for record in records:
                if record.kind == "measurements":
                    continue
                existing = records_by_id.get(record.artifact_id)
                if (
                    existing is not None
                    and record.kind == "scalar_field"
                    and existing.sha256 != record.sha256
                ):
                    return None
                records_by_id.setdefault(record.artifact_id, record)
        output_dir = project_dir / "runs" / run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        restored: list[ArtifactRecord] = []
        for record in records_by_id.values():
            source = (project_dir / record.relative_path).resolve()
            if not self._valid_source(project_dir, source, record):
                return None
            target = output_dir / source.name
            try:
                document = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(document, dict):
                return None
            document["run_id"] = run_id
            target.write_text(
                json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            restored.append(
                ArtifactRecord(
                    record.artifact_id,
                    record.kind,
                    target.relative_to(project_dir).as_posix(),
                    record.media_type,
                    _utc_now(),
                    run_id=run_id,
                    logical_id=record.logical_id or record.artifact_id,
                    size_bytes=target.stat().st_size,
                    sha256=self._sha256(target),
                )
            )
        measurement_template = None
        combined_measurements = []
        measurement_record = next(iter(measurement_sources.values()))
        for operation_id, record in measurement_sources.items():
            source = (project_dir / record.relative_path).resolve()
            if not self._valid_source(project_dir, source, record):
                return None
            try:
                document = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(document, dict):
                return None
            if measurement_template is None:
                measurement_template = dict(document)
            combined_measurements.extend(
                item
                for item in document.get("measurements", [])
                if isinstance(item, dict) and item.get("operation_id") == operation_id
            )
        if measurement_template is None:
            return None
        measurement_template["run_id"] = run_id
        measurement_template["measurements"] = combined_measurements
        measurement_target = output_dir / Path(measurement_record.relative_path).name
        measurement_target.write_text(
            json.dumps(measurement_template, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        restored.append(
            ArtifactRecord(
                measurement_record.artifact_id,
                measurement_record.kind,
                measurement_target.relative_to(project_dir).as_posix(),
                measurement_record.media_type,
                _utc_now(),
                run_id=run_id,
                logical_id=measurement_record.logical_id
                or measurement_record.artifact_id,
                size_bytes=measurement_target.stat().st_size,
                sha256=self._sha256(measurement_target),
            )
        )
        try:
            validate_objective_result(
                plan.operations,
                project_dir,
                restored,
                run_id=run_id,
                input_sha256=input_sha256,
                process=plan.process,
                scope_id=plan.scope_id,
                regions=plan.regions,
                error_code="objective_cache_invalid",
            )
        except DFMError:
            return None
        return restored

    @classmethod
    def _valid_source(
        cls, project_dir: Path, source: Path, record: ArtifactRecord
    ) -> bool:
        return (
            record.kind in _OBJECTIVE_KINDS
            and source.suffix.lower() == ".json"
            and source.is_relative_to(project_dir.resolve())
            and source.is_file()
            and source.stat().st_size == record.size_bytes
            and cls._sha256(source) == record.sha256
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
