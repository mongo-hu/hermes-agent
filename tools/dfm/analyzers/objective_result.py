"""Backend-neutral validation for objective geometry result artifacts."""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path

from ..contracts import ArtifactRecord, PlanOperation, RegionRecord
from ..errors import DFMError


def validate_objective_result(
    operations: list[PlanOperation],
    project_dir: Path,
    artifacts: list[ArtifactRecord],
    *,
    run_id: str,
    input_sha256: str,
    process: str,
    scope_id: str,
    regions: list[RegionRecord] | None = None,
    error_code: str = "objective_result_invalid",
) -> None:
    """Validate the contract shared by all objective geometry backends."""

    measurements = next(
        (item for item in artifacts if item.kind == "measurements"), None
    )
    if measurements is None:
        raise DFMError(error_code, "Objective result must include measurements.")
    by_id = {item.artifact_id: item for item in artifacts}
    payload = _read(project_dir, measurements, error_code)
    if (
        payload.get("schema_version") != 1
        or payload.get("producer_contract") != "measurement_only"
        or not isinstance(payload.get("measurements"), list)
        or payload.get("run_id") != run_id
        or payload.get("input_sha256") != input_sha256
        or payload.get("process") != process
        or payload.get("scope_id") != scope_id
    ):
        raise DFMError(
            error_code,
            "Measurements do not implement the objective production contract.",
        )

    task_operations = {item.operation_id: item for item in operations}
    planned_snapshot_ids = {
        ref.topology_snapshot_id
        for region in regions or []
        for ref in [*region.geometry_refs, *region.excluded_geometry_refs]
    }
    expected = {
        (operation.operation_id, metric_id, quantity_id)
        for operation in operations
        for metric_id in operation.metric_ids
        for quantity_id in operation.required_quantities
    }
    received: set[tuple[str, str, str]] = set()
    for measurement in payload["measurements"]:
        if not isinstance(measurement, dict):
            raise DFMError(error_code, "Measurements must contain JSON objects.")
        operation_id = str(measurement.get("operation_id") or "")
        metric_id = str(measurement.get("metric_id") or "")
        quantity_id = str(measurement.get("quantity_id") or "")
        operation = task_operations.get(operation_id)
        if (
            operation is None
            or measurement.get("calculator_id") != operation.calculator_id
            or metric_id not in operation.metric_ids
            or quantity_id not in operation.required_quantities
            or measurement.get("input_sha256") != input_sha256
            or sorted(measurement.get("feature_refs") or [])
            != sorted(operation.feature_refs)
            or sorted(measurement.get("region_refs") or [])
            != sorted(operation.region_refs)
        ):
            raise DFMError(
                error_code,
                "Measurement does not link to its submitted task contract.",
                {"measurement_id": measurement.get("measurement_id")},
            )
        field_refs = measurement.get("field_refs")
        if not isinstance(field_refs, list) or any(
            not isinstance(ref, str)
            or ref not in by_id
            or by_id[ref].kind != "scalar_field"
            for ref in field_refs
        ):
            raise DFMError(error_code, "Measurement field_refs do not resolve.")
        if "scalar_field" in operation.required_artifacts and not field_refs:
            raise DFMError(error_code, "A field-backed measurement has no field_ref.")
        if any(
            not isinstance(ref, dict)
            or ref.get("input_sha256") != input_sha256
            or not ref.get("topology_snapshot_id")
            or not ref.get("entity_id")
            or (
                planned_snapshot_ids
                and ref.get("topology_snapshot_id") not in planned_snapshot_ids
            )
            for ref in measurement.get("geometry_refs") or []
        ):
            raise DFMError(error_code, "Measurement geometry belongs to another input.")
        received.add((operation_id, metric_id, quantity_id))
    missing = sorted(expected - received)
    if missing:
        raise DFMError(
            error_code,
            "Objective measurements are missing required metric results.",
            {"missing_operation_metrics": missing},
        )

    required_kinds = {
        kind for operation in operations for kind in operation.required_artifacts
    }
    missing_kinds = sorted(required_kinds - {item.kind for item in artifacts})
    if missing_kinds:
        raise DFMError(
            error_code,
            "Objective results are missing required geometry artifacts.",
            {"missing_artifact_kinds": missing_kinds},
        )
    linked_payloads = {
        artifact.artifact_id: _read(project_dir, artifact, error_code)
        for artifact in artifacts
        if artifact.kind in {"scalar_field", "render_scene", "topology_map"}
    }
    if any(
        item.get("schema_version") != 2
        or item.get("run_id") != run_id
        or item.get("input_sha256") != input_sha256
        for item in linked_payloads.values()
    ):
        raise DFMError(error_code, "Objective geometry belongs to another run or input.")

    scenes = [item for item in linked_payloads.values() if item.get("scene_id")]
    topology_maps = [item for item in linked_payloads.values() if item.get("map_id")]
    for scene in scenes:
        snapshot = scene.get("render_mesh_snapshot")
        if not isinstance(snapshot, dict):
            raise DFMError(error_code, "Render scene has no immutable mesh snapshot.")
        mesh_id = str(snapshot.get("render_mesh_snapshot_id") or "")
        primitives = scene.get("primitives")
        if (
            not mesh_id
            or not isinstance(primitives, list)
            or snapshot.get("input_sha256") != input_sha256
            or snapshot.get("topology_snapshot_id") != scene.get("topology_snapshot_ref")
            or any(item.get("render_mesh_snapshot_id") != mesh_id for item in primitives)
            or snapshot.get("triangle_count")
            != sum(len(item.get("triangles", [])) for item in primitives)
            or snapshot.get("render_mesh_sha256") != _mesh_content_sha256(primitives)
        ):
            raise DFMError(error_code, "Render mesh snapshot identity or content is invalid.")
    for topology in topology_maps:
        snapshot = topology.get("topology_snapshot")
        if not isinstance(snapshot, dict):
            raise DFMError(error_code, "Topology map has no immutable topology snapshot.")
        topology_id = str(snapshot.get("topology_snapshot_id") or "")
        faces = topology.get("faces")
        if (
            not topology_id
            or not isinstance(faces, list)
            or snapshot.get("input_sha256") != input_sha256
            or snapshot.get("topology_content_sha256") != _topology_content_sha256(faces)
            or snapshot.get("entity_count", {}).get("face") != len(faces)
            or any(
                face.get("geometry_ref", {}).get("topology_snapshot_id") != topology_id
                or not face.get("geometry_ref", {}).get("entity_id")
                or face.get("geometry_ref", {}).get("input_sha256") != input_sha256
                for face in faces
            )
        ):
            raise DFMError(error_code, "Topology snapshot identity or content is invalid.")
        scene = linked_payloads.get(str(topology.get("scene_ref") or ""), {})
        mesh_id = str(scene.get("render_mesh_snapshot", {}).get("render_mesh_snapshot_id") or "")
        if (
            scene.get("topology_snapshot_ref") != topology_id
            or topology.get("render_mesh_snapshot_ref") != mesh_id
            or any(
                ref.get("render_mesh_snapshot_id") != mesh_id
                for face in faces
                for ref in face.get("triangle_refs", [])
            )
        ):
            raise DFMError(error_code, "Topology and render mesh snapshots are inconsistent.")
        if planned_snapshot_ids and planned_snapshot_ids != {topology_id}:
            raise DFMError(
                error_code,
                "Planned feature regions and objective results use different topology snapshots.",
            )

    for artifact in artifacts:
        if artifact.kind != "scalar_field":
            continue
        field = linked_payloads[artifact.artifact_id]
        operation = task_operations.get(str(field.get("operation_id") or ""))
        if (
            operation is None
            or field.get("metric_id") not in operation.metric_ids
            or field.get("quantity_id") not in operation.required_quantities
            or sorted(field.get("feature_refs") or [])
            != sorted(operation.feature_refs)
            or sorted(field.get("region_refs") or [])
            != sorted(operation.region_refs)
        ):
            raise DFMError(error_code, "Scalar field does not link to its operation.")
        calculation_context = field.get("calculation_context")
        if not isinstance(calculation_context, dict) or set(calculation_context) - {
            "pull_direction"
        }:
            raise DFMError(error_code, "Scalar field calculation context is invalid.")
        pull_direction = calculation_context.get("pull_direction")
        if field.get("quantity_id") == "draft_angle_deg":
            if not _is_unit_vector(pull_direction):
                raise DFMError(
                    error_code,
                    "Draft scalar field must include a normalized pull direction.",
                )
        elif pull_direction is not None and not _is_unit_vector(pull_direction):
            raise DFMError(error_code, "Scalar field pull direction is invalid.")
        scene_ref = str(field.get("scene_ref") or "")
        topology_ref = str(field.get("topology_map_ref") or "")
        if (
            scene_ref not in by_id
            or by_id[scene_ref].kind != "render_scene"
            or topology_ref not in by_id
            or by_id[topology_ref].kind != "topology_map"
            or linked_payloads.get(topology_ref, {}).get("scene_ref") != scene_ref
        ):
            raise DFMError(error_code, "Scalar field scene/topology refs are inconsistent.")
        scene = linked_payloads[scene_ref]
        topology = linked_payloads[topology_ref]
        topology_id = str(topology.get("topology_snapshot", {}).get("topology_snapshot_id") or "")
        mesh_id = str(scene.get("render_mesh_snapshot", {}).get("render_mesh_snapshot_id") or "")
        if (
            field.get("topology_snapshot_ref") != topology_id
            or field.get("render_mesh_snapshot_ref") != mesh_id
            or any(
                item.get("geometry_ref", {}).get("topology_snapshot_id") != topology_id
                for item in [*field.get("samples", []), *field.get("cells", [])]
            )
            or any(
                item.get("triangle_ref", {}).get("render_mesh_snapshot_id") != mesh_id
                for item in field.get("cells", [])
            )
        ):
            raise DFMError(error_code, "Scalar field snapshot refs are inconsistent.")
        sample_ids = {
            str(item.get("sample_id"))
            for item in field.get("samples", [])
            if isinstance(item, dict) and item.get("sample_id")
        }
        if any(
            not isinstance(cell, dict)
            or not set(str(value) for value in cell.get("sample_ids", [])).issubset(
                sample_ids
            )
            for cell in field.get("cells", [])
        ):
            raise DFMError(error_code, "Scalar field cells reference missing samples.")


def _read(
    project_dir: Path, artifact: ArtifactRecord, error_code: str
) -> dict:
    try:
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DFMError(error_code, f"The {artifact.kind} artifact is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise DFMError(error_code, f"The {artifact.kind} artifact must be an object.")
    return payload


def _is_unit_vector(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 3:
        return False
    try:
        components = [float(item) for item in value]
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(item) for item in components):
        return False
    return abs(math.sqrt(sum(item * item for item in components)) - 1.0) <= 1e-6


def _stable_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _mesh_content_sha256(primitives: list[dict]) -> str:
    return _stable_sha256(
        [{key: value for key, value in item.items() if key != "render_mesh_snapshot_id"} for item in primitives]
    )


def _topology_content_sha256(faces: list[dict]) -> str:
    return _stable_sha256(
        [
            {
                "entity_id": face.get("geometry_ref", {}).get("entity_id"),
                "kind": face.get("geometry_ref", {}).get("kind"),
                "index": face.get("geometry_ref", {}).get("index"),
            }
            for face in faces
        ]
    )
