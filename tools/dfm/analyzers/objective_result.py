"""Backend-neutral validation for objective geometry result artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..contracts import ArtifactRecord, PlanOperation
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
    error_code: str = "objective_result_invalid",
) -> None:
    """Validate the backend-neutral objective geometry artifact contract."""

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

    metric_fields_artifact = next(
        (item for item in artifacts if item.kind == "metric_fields"), None
    )
    metric_field_views: dict[str, dict] = {}
    metric_fields_by_id: dict[str, dict] = {}
    if metric_fields_artifact is not None:
        metric_fields_payload = _read(project_dir, metric_fields_artifact, error_code)
        fields = metric_fields_payload.get("fields")
        views = metric_fields_payload.get("views")
        if (
            metric_fields_payload.get("schema_version") != 1
            or metric_fields_payload.get("run_id") != run_id
            or metric_fields_payload.get("input_sha256") != input_sha256
            or metric_fields_payload.get("process") != process
            or metric_fields_payload.get("scope_id") != scope_id
            or not isinstance(fields, list)
            or not isinstance(views, list)
        ):
            raise DFMError(error_code, "Metric fields have an invalid identity.")
        metric_fields_by_id = {
            str(item.get("field_id")): item
            for item in fields
            if isinstance(item, dict) and item.get("field_id")
        }
        metric_field_views = {
            str(item.get("field_id")): item
            for item in views
            if isinstance(item, dict) and item.get("field_id")
        }
        if len(metric_fields_by_id) != len(fields) or len(metric_field_views) != len(views):
            raise DFMError(error_code, "Metric field identities must be unique.")
        if any(
            str(view.get("source_field_id") or "") not in metric_fields_by_id
            for view in metric_field_views.values()
        ):
            raise DFMError(error_code, "Metric field views do not resolve.")

    task_operations = {item.operation_id: item for item in operations}
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
        ):
            raise DFMError(
                error_code,
                "Measurement does not link to its submitted task contract.",
                {"measurement_id": measurement.get("measurement_id")},
            )
        field_refs = measurement.get("field_refs")
        if not isinstance(field_refs, list) or any(
            not isinstance(ref, str)
            or (
                (ref not in by_id or by_id[ref].kind != "scalar_field")
                and ref not in metric_field_views
            )
            for ref in field_refs
        ):
            raise DFMError(error_code, "Measurement field_refs do not resolve.")
        for ref in field_refs:
            if ref not in metric_field_views:
                continue
            view = metric_field_views[ref]
            source = metric_fields_by_id[str(view["source_field_id"])]
            if (
                view.get("quantity_id") != quantity_id
                or source.get("operation_id") != operation_id
                or source.get("calculator_id") != operation.calculator_id
                or source.get("metric_id") != metric_id
                or source.get("input_sha256") != input_sha256
            ):
                raise DFMError(
                    error_code,
                    "Measurement metric-field view does not match its operation.",
                )
        if "scalar_field" in operation.required_artifacts and not field_refs:
            raise DFMError(error_code, "A field-backed measurement has no field_ref.")
        if any(
            not isinstance(ref, dict) or ref.get("input_sha256") != input_sha256
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
    available_kinds = {item.kind for item in artifacts}
    if "metric_fields" in available_kinds:
        available_kinds.add("scalar_field")
    missing_kinds = sorted(required_kinds - available_kinds)
    if missing_kinds:
        raise DFMError(
            error_code,
            "Objective results are missing required geometry artifacts.",
            {"missing_artifact_kinds": missing_kinds},
        )
    linked_payloads = {
        artifact.artifact_id: _read(project_dir, artifact, error_code)
        for artifact in artifacts
        if artifact.kind
        in {
            "scalar_field",
            "metric_fields",
            "render_scene",
            "topology_map",
            "preflight",
            "features",
        }
    }
    if any(
        item.get("schema_version") != 1
        or item.get("run_id") != run_id
        or item.get("input_sha256") != input_sha256
        for item in linked_payloads.values()
    ):
        raise DFMError(error_code, "Objective geometry belongs to another run or input.")

    topology_artifact = next(
        (item for item in artifacts if item.kind == "topology_map"), None
    )
    if topology_artifact is not None:
        topology = linked_payloads[topology_artifact.artifact_id]
        face_indices = _topology_indices(topology.get("faces"), error_code)
        edge_indices = _topology_indices(topology.get("edges"), error_code)
        _validate_topology_references(
            topology,
            face_indices,
            edge_indices,
            error_code,
        )
        for measurement in payload["measurements"]:
            _validate_geometry_refs(
                measurement.get("geometry_refs"),
                input_sha256,
                face_indices,
                edge_indices,
                error_code,
            )

    feature_artifacts = [item for item in artifacts if item.kind == "features"]
    if len(feature_artifacts) > 1:
        raise DFMError(error_code, "Objective result contains duplicate feature artifacts.")
    if feature_artifacts:
        features = linked_payloads[feature_artifacts[0].artifact_id]
        if (
            features.get("schema_version") != 1
            or features.get("run_id") != run_id
            or features.get("input_sha256") != input_sha256
            or features.get("process") != process
            or features.get("scope_id") != scope_id
            or not isinstance(features.get("features"), list)
        ):
            raise DFMError(error_code, "Feature artifact identity is invalid.")
        feature_ids: set[str] = set()
        for feature in features["features"]:
            if not isinstance(feature, dict):
                raise DFMError(error_code, "Feature artifact entries must be objects.")
            feature_id = str(feature.get("feature_id") or "")
            confidence = feature.get("confidence")
            if (
                not feature_id
                or feature_id in feature_ids
                or feature.get("input_sha256") != input_sha256
                or not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not 0 <= float(confidence) <= 1
            ):
                raise DFMError(error_code, "Feature record identity is invalid.")
            feature_ids.add(feature_id)
            if topology_artifact is not None:
                _validate_geometry_refs(
                    feature.get("geometry_refs"),
                    input_sha256,
                    face_indices,
                    edge_indices,
                    error_code,
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


def _topology_indices(values: object, error_code: str) -> set[int]:
    if not isinstance(values, list):
        raise DFMError(error_code, "Topology index collections must be arrays.")
    indices = {
        item.get("index")
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("index"), int)
        and not isinstance(item.get("index"), bool)
        and item["index"] > 0
    }
    if len(indices) != len(values):
        raise DFMError(
            error_code,
            "Topology indices must be positive and unique.",
        )
    if indices != set(range(1, len(values) + 1)):
        raise DFMError(
            error_code,
            "Topology indices must be contiguous and 1-based.",
        )
    return indices


def _validate_topology_references(
    topology: dict,
    face_indices: set[int],
    edge_indices: set[int],
    error_code: str,
) -> None:
    if topology.get("index_base") != 1:
        raise DFMError(error_code, "Topology index_base must be 1.")
    edges = topology.get("edges")
    arcs = topology.get("aag")
    if not isinstance(edges, list) or not isinstance(arcs, list):
        raise DFMError(error_code, "Topology edges and AAG must be arrays.")
    for edge in edges:
        adjacent = edge.get("adjacent_face_indices") if isinstance(edge, dict) else None
        if (
            not isinstance(adjacent, list)
            or any(
                not isinstance(index, int)
                or isinstance(index, bool)
                or index not in face_indices
                for index in adjacent
            )
            or len(adjacent) != len(set(adjacent))
        ):
            raise DFMError(error_code, "Topology edge adjacency does not resolve.")
    seen_arcs: set[tuple[int, int]] = set()
    for arc in arcs:
        arc_edges = arc.get("edge_indices") if isinstance(arc, dict) else None
        faces = arc.get("face_indices") if isinstance(arc, dict) else None
        if (
            not isinstance(arc_edges, list)
            or not arc_edges
            or any(
                not isinstance(index, int)
                or isinstance(index, bool)
                or index not in edge_indices
                for index in arc_edges
            )
            or len(arc_edges) != len(set(arc_edges))
            or not isinstance(faces, list)
            or len(faces) != 2
            or any(
                not isinstance(index, int)
                or isinstance(index, bool)
                or index not in face_indices
                for index in faces
            )
            or len(set(faces)) != 2
        ):
            raise DFMError(error_code, "Topology AAG references do not resolve.")
        arc_identity = tuple(sorted(faces))
        if arc_identity in seen_arcs:
            raise DFMError(error_code, "Topology AAG contains a duplicate face arc.")
        seen_arcs.add(arc_identity)


def _validate_geometry_refs(
    values: object,
    input_sha256: str,
    face_indices: set[int],
    edge_indices: set[int],
    error_code: str,
) -> None:
    if not isinstance(values, list):
        raise DFMError(error_code, "Geometry references must be an array.")
    for item in values:
        if not isinstance(item, dict) or item.get("input_sha256") != input_sha256:
            raise DFMError(error_code, "Geometry reference belongs to another input.")
        kind = item.get("kind")
        index = item.get("index")
        allowed = face_indices if kind == "face" else edge_indices if kind == "edge" else None
        if (
            allowed is None
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index not in allowed
        ):
            raise DFMError(error_code, "Geometry reference does not resolve in topology.")
