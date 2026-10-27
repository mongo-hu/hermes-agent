"""Turn evaluated scalar fields into precisely linked evidence images."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import operator
from pathlib import Path
from typing import Any, Callable

from ..contracts import (
    ArtifactRecord,
    EvidenceRecord,
    GeometricEvidenceRecord,
    GeometryRef,
)
from ..errors import DFMError
from ..geometry.snapshot_hash import render_mesh_content_sha256
from .planner import (
    PreparedScene,
    ProjectedScene,
    plan_evidence_views,
    prepare_scene,
    project_scene,
    projection_cache_key,
)


EVIDENCE_SCHEMA_VERSION = 3


def _between(value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        lower, upper = expected.get("lower"), expected.get("upper")
    elif isinstance(expected, (list, tuple)) and len(expected) == 2:
        lower, upper = expected
    else:
        return False
    return bool(lower <= value <= upper)


_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
    "between": _between,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FieldEvidenceEngine:
    """Render precise evidence from any backend's objective scalar fields."""

    version = "hermes-field-evidence-v8"

    def materialize(
        self,
        project_dir: Path,
        run_id: str,
        artifacts: list[ArtifactRecord],
        *,
        max_markers_per_image: int = 10,
    ) -> list[ArtifactRecord]:
        by_kind = {item.kind: item for item in artifacts}
        measurements_artifact = by_kind.get("geometric_results") or by_kind.get(
            "measurements"
        )
        evaluations_artifact = by_kind.get("evaluations")
        if measurements_artifact is None or evaluations_artifact is None:
            return []

        payload_cache: dict[str, dict[str, Any]] = {}
        validation_cache: dict[tuple[str, str], dict[str, Any]] = {}
        measurements_payload = _read_json(
            project_dir, measurements_artifact, payload_cache
        )
        evaluations_payload = _read_json(
            project_dir, evaluations_artifact, payload_cache
        )
        input_sha256 = str(measurements_payload.get("input_sha256") or "")
        if evaluations_payload.get("run_id") != run_id:
            raise DFMError(
                "evidence_input_invalid",
                "The evaluation artifact belongs to a different run.",
            )
        if measurements_artifact.kind == "geometric_results":
            rows = []
            for item in measurements_payload.get("results", []):
                if not isinstance(item, dict) or not item.get("result_id"):
                    continue
                evidence = item.get("evidence") or {}
                rows.append({
                    **item,
                    "measurement_id": item["result_id"],
                    "feature_refs": [item.get("feature_id")],
                    "region_refs": [],
                    "field_refs": list(evidence.get("field_refs") or []),
                    "geometry_refs": list(evidence.get("geometry_refs") or []),
                })
        else:
            rows = measurements_payload.get("measurements", [])
        measurements = {
            str(item.get("measurement_id")): item
            for item in rows
            if isinstance(item, dict) and item.get("measurement_id")
        }
        artifact_by_id = {item.artifact_id: item for item in artifacts}
        # Worker payload references are distinct from Hermes run artifact IDs.
        # Keep both identities without rewriting immutable geometry artifacts.
        for artifact in artifacts:
            id_key = {"render_scene": "scene_id", "topology_map": "map_id"}.get(
                artifact.kind
            )
            if id_key is None:
                continue
            payload = _read_json(project_dir, artifact, payload_cache)
            reference = payload.get(id_key)
            if not isinstance(reference, str) or not reference:
                raise DFMError(
                    "evidence_artifact_invalid",
                    f"The {artifact.kind} artifact has no valid {id_key}.",
                )
            existing = artifact_by_id.get(reference)
            if existing is not None and existing != artifact:
                raise DFMError(
                    "evidence_artifact_invalid",
                    "A shared geometry reference resolves to multiple artifacts.",
                    {"reference": reference},
                )
            artifact_by_id[reference] = artifact
        pull_direction = _field_pull_direction(project_dir, artifacts, payload_cache)
        patches: list[dict[str, Any]] = []
        for evaluation in evaluations_payload.get("evaluations", []):
            if not isinstance(evaluation, dict) or evaluation.get("outcome") != "fail":
                continue
            expression = evaluation.get("expression")
            direct_expression = expression is None or (
                isinstance(expression, dict)
                and set(expression) == {"operand"}
                and isinstance(expression.get("operand"), str)
                and expression["operand"] in (evaluation.get("operand_values") or {})
            )
            comparison = _OPERATORS.get(str(evaluation.get("operator") or ""))
            if comparison is None:
                raise DFMError(
                    "evidence_rule_invalid",
                    "Evidence geometry cannot apply the evaluation operator.",
                )
            linked = [
                measurements[item]
                for item in evaluation.get("measurement_ids", [])
                if item in measurements
            ]
            composite_fields: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
            for measurement in linked:
                for field_ref in measurement.get("field_refs", []):
                    field_artifact = artifact_by_id.get(str(field_ref))
                    if field_artifact is None or field_artifact.kind != "scalar_field":
                        raise DFMError(
                            "evidence_field_missing",
                            "A failed measurement references a missing scalar field.",
                            {"field_ref": field_ref},
                        )
                    field = _read_json(project_dir, field_artifact, payload_cache)
                    self._validate_field_identity(
                        project_dir,
                        field,
                        run_id,
                        input_sha256,
                        measurement,
                        artifact_by_id,
                        payload_cache,
                        validation_cache,
                    )
                    scene = _read_json(
                        project_dir,
                        artifact_by_id[str(field.get("scene_ref") or "")],
                        payload_cache,
                    )
                    if direct_expression:
                        patches.extend(
                            self._failed_patches(
                                evaluation,
                                measurement,
                                field_ref=str(field_ref),
                                field=field,
                                scene=scene,
                                comparison=comparison,
                            )
                        )
                    else:
                        composite_fields.append((measurement, str(field_ref), field))
            if composite_fields:
                patches.extend(self._composite_patches(evaluation, composite_fields))

        output_dir = project_dir / "runs" / run_id / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        geometry_path = output_dir / "evidence_geometry.json"
        geometry_path.write_text(
            json.dumps(
                {
                    "schema_version": EVIDENCE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "input_sha256": input_sha256,
                    "producer": "hermes-evidence-engine",
                    "producer_version": self.version,
                    "failed_patches": patches,
                    "coverage_requirements": _coverage_requirements(patches),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        generated = [
            ArtifactRecord(
                f"artifact_{run_id}_evidence_geometry",
                "evidence_geometry",
                geometry_path.relative_to(project_dir).as_posix(),
                "application/json",
                _utc_now(),
            )
        ]

        records: list[EvidenceRecord | GeometricEvidenceRecord] = []
        evaluation_by_id = {
            str(item.get("evaluation_id")): item
            for item in evaluations_payload.get("evaluations", [])
            if isinstance(item, dict)
        }
        image_index = 0
        covered_patch_ids: set[str] = set()
        prepared_by_scene: dict[str, PreparedScene] = {}
        projected_by_camera: dict[tuple[str, tuple[float, ...]], ProjectedScene] = {}
        patches_by_scene: dict[str, list[dict[str, Any]]] = {}
        for patch in patches:
            patches_by_scene.setdefault(str(patch["scene_ref"]), []).append(patch)

        for scene_ref in sorted(patches_by_scene):
            scene_artifact = artifact_by_id[scene_ref]
            scene = _read_json(project_dir, scene_artifact, payload_cache)
            prepared = prepared_by_scene.setdefault(scene_ref, prepare_scene(scene))
            scene_patches = patches_by_scene[scene_ref]
            patch_by_id = {str(patch["patch_id"]): patch for patch in scene_patches}
            patches_by_evaluation: dict[str, list[dict[str, Any]]] = {}
            for patch in scene_patches:
                patches_by_evaluation.setdefault(
                    str(patch["evaluation_id"]), []
                ).append(patch)

            # Evidence is an issue-local artifact. Planning each evaluation
            # independently prevents one overview image from being reused by
            # unrelated report issues while still allowing adjacent patches of
            # the same issue to share one readable close-up.
            plans = [
                plan
                for evaluation_id in sorted(patches_by_evaluation)
                for plan in plan_evidence_views(
                    prepared,
                    patches_by_evaluation[evaluation_id],
                    pull_direction,
                    max_markers_per_image=max(1, max_markers_per_image),
                )
            ]
            for plan in plans:
                selected = [patch_by_id[patch_id] for patch_id in plan.patch_ids]
                selected_evaluation_ids = {
                    str(patch["evaluation_id"]) for patch in selected
                }
                if len(selected_evaluation_ids) != 1:
                    raise DFMError(
                        "evidence_issue_scope_invalid",
                        "An evidence image must contain patches from exactly one evaluation.",
                        {"evaluation_ids": sorted(selected_evaluation_ids)},
                    )
                evaluation_id = next(iter(selected_evaluation_ids))
                evaluation = evaluation_by_id[evaluation_id]
                image_index += 1
                image_id = f"artifact_{run_id}_evidence_{image_index}"
                image_path = output_dir / f"evidence_{image_index:03d}.png"
                camera_key = (scene_ref, projection_cache_key(plan.view))
                projected = projected_by_camera.get(camera_key)
                if projected is None:
                    projected = project_scene(prepared, plan.view)
                    projected_by_camera[camera_key] = projected
                self._render(prepared, projected, selected, image_path, plan.view)
                image_artifact = ArtifactRecord(
                    image_id,
                    "evidence_image",
                    image_path.relative_to(project_dir).as_posix(),
                    "image/png",
                    _utc_now(),
                )
                generated.append(image_artifact)
                covered_patch_ids.update(plan.patch_ids)
                marker_labels = {
                    str(patch["patch_id"]): f"ISSUE {ordinal:02d}"
                    for ordinal, patch in enumerate(selected, start=1)
                }

                geometry_refs = _unique_dicts([
                    geometry_ref
                    for patch in selected
                    for geometry_ref in patch["geometry_refs"]
                ])
                common_evidence = {
                    "evidence_id": f"evidence_{run_id}_{image_index}_1",
                    "run_id": run_id,
                    "input_sha256": input_sha256,
                    "measurement_ids": sorted({
                        str(item)
                        for patch in selected
                        for item in patch["measurement_ids"]
                    }),
                    "evaluation_ids": [evaluation_id],
                    "geometry_refs": [
                        GeometryRef.from_dict(item) for item in geometry_refs
                    ],
                    "artifact_ref": image_id,
                    "render": {
                        "mode": "local_issue",
                        "producer": "hermes-evidence-renderer",
                        "version": self.version,
                        "viewport": [1440, 810],
                        "covered_patch_ids": [
                            str(patch["patch_id"]) for patch in selected
                        ],
                        "covered_sample_refs": sorted({
                            str(item)
                            for patch in selected
                            for item in patch["sample_refs"]
                        }),
                        "coverage_modes": sorted({
                            str(patch["coverage_mode"]) for patch in selected
                        }),
                        "markers": [
                            {
                                "label": marker_labels[str(patch["patch_id"])],
                                "patch_id": str(patch["patch_id"]),
                            }
                            for patch in selected
                        ],
                        "scene_ref": scene_ref,
                        "topology_snapshot_ref": str(
                            selected[0]["topology_snapshot_ref"]
                        ),
                        "render_mesh_snapshot_ref": str(
                            selected[0]["render_mesh_snapshot_ref"]
                        ),
                        "view_id": plan.view["id"],
                        "view_label": plan.view["label"],
                        "camera_direction": list(plan.view["basis_d"]),
                        "camera_up": list(plan.view["basis_v"]),
                        "camera_source": plan.view["source"],
                        "presentation_mode": "local",
                        "visibility_mode": plan.visibility_mode,
                    },
                    "feature_refs": sorted({
                        str(item)
                        for patch in selected
                        for item in patch["feature_refs"]
                    }),
                }
                if evaluation.get("geometric_id"):
                    records.append(
                        GeometricEvidenceRecord(
                            geometric_id=str(evaluation["geometric_id"]),
                            **common_evidence,
                        )
                    )
                else:
                    records.append(
                        EvidenceRecord(
                            operation_id=str(evaluation.get("operation_id") or ""),
                            metric_id=str(evaluation.get("metric_id") or ""),
                            region_refs=sorted({
                                str(item)
                                for patch in selected
                                for item in patch["region_refs"]
                            }),
                            **common_evidence,
                        )
                    )

        coverage = _coverage_summary(patches, covered_patch_ids)
        if coverage["uncovered_patch_ids"] or coverage["uncovered_sample_refs"]:
            raise DFMError(
                "evidence_coverage_incomplete",
                "Evidence rendering did not cover every failed geometry patch.",
                coverage,
            )

        records_path = output_dir / "evidence_records.json"
        records_path.write_text(
            json.dumps(
                {
                    "schema_version": EVIDENCE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "input_sha256": input_sha256,
                    "producer": "hermes-evidence-renderer",
                    "producer_version": self.version,
                    "coverage": coverage,
                    "records": [item.to_dict() for item in records],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        generated.append(
            ArtifactRecord(
                f"artifact_{run_id}_evidence_records",
                "evidence_records",
                records_path.relative_to(project_dir).as_posix(),
                "application/json",
                _utc_now(),
            )
        )
        return generated

    @staticmethod
    def _validate_field_identity(
        project_dir: Path,
        field: dict[str, Any],
        run_id: str,
        input_sha256: str,
        measurement: dict[str, Any],
        artifact_by_id: dict[str, ArtifactRecord],
        payload_cache: dict[str, dict[str, Any]],
        validation_cache: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        if measurement.get("geometric_id"):
            valid_identity = (
                field.get("contract_version") == "dfm.geometry/v2"
                and field.get("snapshot_id") == measurement.get("snapshot_id")
                and field.get("input_sha256") == input_sha256
                and field.get("feature_id") == measurement.get("feature_id")
                and measurement.get("geometric_id") in field.get("geometric_ids", [])
                and field.get("context") == measurement.get("context")
            )
        else:
            valid_identity = (
                field.get("run_id"),
                field.get("input_sha256"),
                field.get("operation_id"),
                field.get("metric_id"),
                field.get("quantity_id"),
            ) == (
                run_id,
                input_sha256,
                measurement.get("operation_id"),
                measurement.get("metric_id"),
                measurement.get("quantity_id"),
            )
        if not valid_identity:
            raise DFMError(
                "evidence_field_invalid",
                "The scalar field does not belong to its linked objective result.",
            )
        linked_payloads: dict[str, dict[str, Any]] = {}
        for ref, kind in (
            (field.get("scene_ref"), "render_scene"),
            (field.get("topology_map_ref"), "topology_map"),
        ):
            artifact = artifact_by_id.get(str(ref))
            if artifact is None or artifact.kind != kind:
                raise DFMError(
                    "evidence_field_invalid",
                    f"The scalar field references a missing {kind} artifact.",
                )
            linked = _read_json(project_dir, artifact, payload_cache)
            linked_payloads[kind] = linked
            if not measurement.get("geometric_id") and linked.get("run_id") != run_id:
                raise DFMError(
                    "evidence_field_invalid",
                    f"The linked {kind} artifact belongs to another run.",
                )
            if linked.get("input_sha256") != input_sha256:
                raise DFMError(
                    "evidence_field_invalid",
                    f"The linked {kind} artifact belongs to another input.",
                )
            if kind == "topology_map" and linked.get("scene_ref") != field.get(
                "scene_ref"
            ):
                raise DFMError(
                    "evidence_field_invalid",
                    "The topology map and scalar field reference different scenes.",
                )
        cache_key = (
            str(field.get("scene_ref") or ""),
            str(field.get("topology_map_ref") or ""),
        )
        snapshot_data = validation_cache.get(cache_key)
        if snapshot_data is None:
            scene_triangles = {
                (
                    str(primitive.get("render_mesh_snapshot_id")),
                    str(primitive.get("primitive_id")),
                    triangle_id,
                )
                for primitive in linked_payloads["render_scene"].get("primitives", [])
                for triangle_id, _triangle in enumerate(primitive.get("triangles", []))
            }
            mapped_triangles = {
                (
                    str(ref.get("render_mesh_snapshot_id")),
                    str(ref.get("primitive_id")),
                    int(ref.get("triangle_id", -1)),
                )
                for face in linked_payloads["topology_map"].get("faces", [])
                for ref in face.get("triangle_refs", [])
            }
            mapped_by_entity = {
                (
                    str(face.get("geometry_ref", {}).get("topology_snapshot_id")),
                    str(face.get("geometry_ref", {}).get("entity_id")),
                    str(ref.get("render_mesh_snapshot_id")),
                    str(ref.get("primitive_id")),
                    int(ref.get("triangle_id", -1)),
                )
                for face in linked_payloads["topology_map"].get("faces", [])
                for ref in face.get("triangle_refs", [])
            }
            scene_snapshot = linked_payloads["render_scene"].get(
                "render_mesh_snapshot", {}
            )
            topology_snapshot = linked_payloads["topology_map"].get(
                "topology_snapshot", {}
            )
            mesh_id = str(scene_snapshot.get("render_mesh_snapshot_id") or "")
            topology_id = str(topology_snapshot.get("topology_snapshot_id") or "")
            if (
                not mesh_id
                or not topology_id
                or linked_payloads["render_scene"].get("topology_snapshot_ref")
                != topology_id
                or linked_payloads["topology_map"].get("render_mesh_snapshot_ref")
                != mesh_id
                or scene_snapshot.get("topology_snapshot_id") != topology_id
                or scene_snapshot.get("input_sha256") != input_sha256
                or topology_snapshot.get("input_sha256") != input_sha256
            ):
                raise DFMError(
                    "evidence_snapshot_mismatch",
                    "Scalar field, topology, and render mesh do not share one immutable snapshot.",
                )
            primitives = linked_payloads["render_scene"].get("primitives", [])
            try:
                mesh_content_sha256 = render_mesh_content_sha256(primitives)
            except ValueError as exc:
                raise DFMError(
                    "evidence_artifact_invalid",
                    "The render mesh snapshot content is not canonicalizable.",
                ) from exc
            topology_payload = [
                {
                    "entity_id": face.get("geometry_ref", {}).get("entity_id"),
                    "kind": face.get("geometry_ref", {}).get("kind"),
                    "index": face.get("geometry_ref", {}).get("index"),
                }
                for face in linked_payloads["topology_map"].get("faces", [])
            ]
            if (
                scene_snapshot.get("render_mesh_sha256") != mesh_content_sha256
                or scene_snapshot.get("triangle_count")
                != sum(len(item.get("triangles", [])) for item in primitives)
                or topology_snapshot.get("topology_content_sha256")
                != _stable_content_sha256(topology_payload)
            ):
                raise DFMError(
                    "evidence_snapshot_mismatch",
                    "The topology or render mesh content no longer matches its immutable snapshot.",
                )
            snapshot_data = {
                "scene_triangles": scene_triangles,
                "mapped_triangles": mapped_triangles,
                "mapped_by_entity": mapped_by_entity,
                "mesh_id": mesh_id,
                "topology_id": topology_id,
            }
            validation_cache[cache_key] = snapshot_data

        scene_triangles = snapshot_data["scene_triangles"]
        mapped_triangles = snapshot_data["mapped_triangles"]
        field_triangles = {
            (
                str(cell.get("triangle_ref", {}).get("render_mesh_snapshot_id")),
                str(cell.get("triangle_ref", {}).get("primitive_id")),
                int(cell.get("triangle_ref", {}).get("triangle_id", -1)),
            )
            for cell in field.get("cells", [])
        }
        if not field_triangles.issubset(scene_triangles & mapped_triangles):
            raise DFMError(
                "evidence_field_invalid",
                "Scalar field cells are not present in both the scene and topology map.",
            )
        mapped_by_entity = snapshot_data["mapped_by_entity"]
        if any(
            (
                str(cell.get("geometry_ref", {}).get("topology_snapshot_id")),
                str(cell.get("geometry_ref", {}).get("entity_id")),
                str(cell.get("triangle_ref", {}).get("render_mesh_snapshot_id")),
                str(cell.get("triangle_ref", {}).get("primitive_id")),
                int(cell.get("triangle_ref", {}).get("triangle_id", -1)),
            )
            not in mapped_by_entity
            for cell in field.get("cells", [])
        ):
            raise DFMError(
                "evidence_field_invalid",
                "A scalar-field cell triangle is mapped to a different topology entity.",
            )
        mesh_id = snapshot_data["mesh_id"]
        topology_id = snapshot_data["topology_id"]
        if (
            field.get("render_mesh_snapshot_ref") != mesh_id
            or field.get("topology_snapshot_ref") != topology_id
        ):
            raise DFMError(
                "evidence_snapshot_mismatch",
                "Scalar field, topology, and render mesh do not share one immutable snapshot.",
            )
        geometry_refs = [
            item.get("geometry_ref", {}) for item in field.get("samples", [])
        ] + [item.get("geometry_ref", {}) for item in field.get("cells", [])]
        if any(
            ref.get("topology_snapshot_id") != topology_id
            or ref.get("input_sha256") != input_sha256
            or not ref.get("entity_id")
            for ref in geometry_refs
        ):
            raise DFMError(
                "evidence_snapshot_mismatch",
                "Scalar field geometry refs do not belong to the linked topology snapshot.",
            )

    @staticmethod
    def _failed_patches(
        evaluation: dict[str, Any],
        measurement: dict[str, Any],
        *,
        field_ref: str,
        field: dict[str, Any],
        scene: dict[str, Any],
        comparison: Callable[[Any, Any], bool],
    ) -> list[dict[str, Any]]:
        expected = evaluation.get("expected")
        samples = {
            str(item.get("sample_id")): item
            for item in field.get("samples", [])
            if isinstance(item, dict) and item.get("sample_id")
        }
        failed_samples = {
            sample_id
            for sample_id, sample in samples.items()
            if not comparison(sample.get("value"), expected)
        }
        if not failed_samples:
            raise DFMError(
                "evidence_field_inconsistent",
                "A failed aggregate measurement has no failing scalar-field sample.",
                {"evaluation_id": evaluation.get("evaluation_id")},
            )
        failed_cells = [
            item
            for item in field.get("cells", [])
            if isinstance(item, dict)
            and failed_samples.intersection(
                str(value) for value in item.get("sample_ids", [])
            )
        ]
        groups = _connected_cells(failed_cells, scene)
        group_specs: list[tuple[list[dict[str, Any]], list[str]]] = []
        grouped_samples: set[str] = set()
        for cells in groups:
            sample_ids = sorted({
                str(sample_id)
                for cell in cells
                for sample_id in cell.get("sample_ids", [])
                if str(sample_id) in failed_samples
            })
            grouped_samples.update(sample_ids)
            group_specs.append((cells, sample_ids))
        for sample_id in sorted(failed_samples - grouped_samples):
            group_specs.append(([], [sample_id]))

        results = []
        for index, (cells, sample_ids) in enumerate(group_specs, start=1):
            points = [samples[item]["point"] for item in sample_ids]
            surface_normal = _average_direction([
                samples[item].get("surface_normal") for item in sample_ids
            ])
            geometry_values = [samples[item]["geometry_ref"] for item in sample_ids] + [
                cell["geometry_ref"] for cell in cells
            ]
            geometry_refs = _unique_dicts(geometry_values)
            triangles = _unique_dicts([cell["triangle_ref"] for cell in cells])
            focus = min(
                (samples[item] for item in sample_ids),
                key=lambda item: float(item.get("value", 0)),
            )["point"]
            if str(evaluation.get("operator")) in {"<=", "<"}:
                focus = max(
                    (samples[item] for item in sample_ids),
                    key=lambda item: float(item.get("value", 0)),
                )["point"]
            stable = hashlib.sha256(
                f"{evaluation.get('evaluation_id')}:{field_ref}:{index}".encode("utf-8")
            ).hexdigest()[:16]
            results.append({
                "patch_id": f"patch_{stable}",
                "evaluation_id": str(evaluation.get("evaluation_id") or ""),
                "coverage_mode": "failed_samples",
                "measurement_ids": [str(measurement.get("measurement_id") or "")],
                "field_ref": field_ref,
                "scene_ref": str(field.get("scene_ref") or ""),
                "topology_map_ref": str(field.get("topology_map_ref") or ""),
                "topology_snapshot_ref": str(field.get("topology_snapshot_ref") or ""),
                "render_mesh_snapshot_ref": str(
                    field.get("render_mesh_snapshot_ref") or ""
                ),
                "geometry_refs": geometry_refs,
                "region_refs": sorted(
                    str(item) for item in measurement.get("region_refs", [])
                ),
                "feature_refs": sorted(
                    str(item) for item in measurement.get("feature_refs", [])
                ),
                "sample_ids": sample_ids,
                "sample_refs": [f"{field_ref}:{sample_id}" for sample_id in sample_ids],
                "cell_ids": sorted(str(item.get("cell_id")) for item in cells),
                "triangle_refs": triangles,
                "focus_point": focus,
                "surface_normal": surface_normal,
                "bounds": {
                    "minimum": [
                        min(point[axis] for point in points) for axis in range(3)
                    ],
                    "maximum": [
                        max(point[axis] for point in points) for axis in range(3)
                    ],
                },
            })
        return results

    @staticmethod
    def _composite_patches(
        evaluation: dict[str, Any],
        fields: list[tuple[dict[str, Any], str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Locate a failed composite result without thresholding its raw operands."""
        grouped: dict[
            tuple[str, str, str, str],
            list[tuple[dict[str, Any], str, dict[str, Any]]],
        ] = {}
        for item in fields:
            field = item[2]
            key = (
                str(field.get("scene_ref") or ""),
                str(field.get("topology_map_ref") or ""),
                str(field.get("topology_snapshot_ref") or ""),
                str(field.get("render_mesh_snapshot_ref") or ""),
            )
            grouped.setdefault(key, []).append(item)

        patches: list[dict[str, Any]] = []
        for group_index, (refs, operands) in enumerate(grouped.items(), start=1):
            points: list[list[float]] = []
            normals: list[Any] = []
            geometry_values: list[dict[str, Any]] = []
            triangle_values: list[dict[str, Any]] = []
            measurement_ids: set[str] = set()
            region_refs: set[str] = set()
            feature_refs: set[str] = set()
            sample_ids: list[str] = []
            cell_ids: list[str] = []
            field_refs: list[str] = []
            for measurement, field_ref, field in operands:
                field_refs.append(field_ref)
                measurement_ids.add(str(measurement.get("measurement_id") or ""))
                region_refs.update(
                    str(item) for item in measurement.get("region_refs", [])
                )
                feature_refs.update(
                    str(item) for item in measurement.get("feature_refs", [])
                )
                geometry_values.extend(
                    item
                    for item in measurement.get("geometry_refs", [])
                    if isinstance(item, dict)
                )
                for sample in field.get("samples", []):
                    if not isinstance(sample, dict):
                        continue
                    point = sample.get("point")
                    if isinstance(point, list) and len(point) == 3:
                        points.append([float(value) for value in point])
                    normals.append(sample.get("surface_normal"))
                    if isinstance(sample.get("geometry_ref"), dict):
                        geometry_values.append(sample["geometry_ref"])
                    if sample.get("sample_id"):
                        sample_ids.append(f"{field_ref}:{sample['sample_id']}")
                for cell in field.get("cells", []):
                    if not isinstance(cell, dict):
                        continue
                    if isinstance(cell.get("geometry_ref"), dict):
                        geometry_values.append(cell["geometry_ref"])
                    if isinstance(cell.get("triangle_ref"), dict):
                        triangle_values.append(cell["triangle_ref"])
                    if cell.get("cell_id"):
                        cell_ids.append(f"{field_ref}:{cell['cell_id']}")

            if not points:
                continue
            stable = hashlib.sha256(
                (
                    f"{evaluation.get('evaluation_id')}:composite:{group_index}:"
                    + ":".join(sorted(field_refs))
                ).encode("utf-8")
            ).hexdigest()[:16]
            patches.append({
                "patch_id": f"patch_{stable}",
                "evaluation_id": str(evaluation.get("evaluation_id") or ""),
                "coverage_mode": "contributing_regions",
                "measurement_ids": sorted(item for item in measurement_ids if item),
                "field_ref": sorted(field_refs)[0],
                "scene_ref": refs[0],
                "topology_map_ref": refs[1],
                "topology_snapshot_ref": refs[2],
                "render_mesh_snapshot_ref": refs[3],
                "geometry_refs": _unique_dicts(geometry_values),
                "region_refs": sorted(region_refs),
                "feature_refs": sorted(feature_refs),
                "sample_ids": sorted(sample_ids),
                "sample_refs": sorted(sample_ids),
                "cell_ids": sorted(cell_ids),
                "triangle_refs": _unique_dicts(triangle_values),
                "focus_point": [
                    sum(point[axis] for point in points) / len(points)
                    for axis in range(3)
                ],
                "surface_normal": _average_direction(normals),
                "bounds": {
                    "minimum": [
                        min(point[axis] for point in points) for axis in range(3)
                    ],
                    "maximum": [
                        max(point[axis] for point in points) for axis in range(3)
                    ],
                },
            })
        return patches

    @staticmethod
    def _render(
        prepared: PreparedScene,
        projected: ProjectedScene,
        patches: list[dict[str, Any]],
        target: Path,
        view: dict[str, Any],
    ) -> None:
        try:
            from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
        except ImportError as exc:
            raise DFMError(
                "evidence_renderer_unavailable",
                "Pillow is required for Hermes field evidence rendering.",
            ) from exc

        final_size = (1440, 810)
        supersample = 2
        width, height = (value * supersample for value in final_size)
        background_top = (249, 250, 252)
        background_bottom = (234, 239, 246)
        panel_top = (247, 250, 253)
        panel_bottom = (226, 233, 242)
        ink = (26, 36, 51)
        muted = (101, 115, 136)
        hairline = (210, 218, 229)
        accent = (229, 72, 66)
        accent_dark = (164, 38, 39)
        accent_pale = (255, 235, 232)

        def font(size: int, *, bold: bool = False) -> Any:
            names = (
                ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
                if bold
                else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
            )
            for name in names:
                try:
                    return ImageFont.truetype(name, size)
                except OSError:
                    continue
            return ImageFont.load_default()

        def vertical_gradient(
            size: tuple[int, int],
            top: tuple[int, int, int],
            bottom: tuple[int, int, int],
        ) -> Any:
            gradient = Image.new("RGB", size, top)
            gradient_draw = ImageDraw.Draw(gradient)
            divisor = max(size[1] - 1, 1)
            for y in range(size[1]):
                amount = y / divisor
                color = tuple(
                    round(start + (end - start) * amount)
                    for start, end in zip(top, bottom)
                )
                gradient_draw.line((0, y, size[0], y), fill=color)
            return gradient

        def paste_rounded(
            canvas: Any,
            layer: Any,
            box: tuple[int, int, int, int],
            *,
            radius: int,
            shadow: bool = True,
        ) -> None:
            left, top, right, bottom = box
            layer_width, layer_height = right - left, bottom - top
            mask = Image.new("L", (layer_width, layer_height), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, layer_width, layer_height), radius=radius, fill=255
            )
            if shadow:
                shadow_mask = Image.new("L", canvas.size, 0)
                ImageDraw.Draw(shadow_mask).rounded_rectangle(
                    (
                        left,
                        top + 8 * supersample,
                        right,
                        bottom + 8 * supersample,
                    ),
                    radius=radius,
                    fill=70,
                )
                shadow_mask = shadow_mask.filter(
                    ImageFilter.GaussianBlur(18 * supersample)
                )
                shadow_layer = Image.new("RGBA", canvas.size, (31, 45, 66, 0))
                shadow_layer.putalpha(shadow_mask)
                canvas.paste(shadow_layer, (0, 0), shadow_layer)
            canvas.paste(layer.resize((layer_width, layer_height)), (left, top), mask)

        if not patches:
            raise DFMError(
                "evidence_patch_invalid", "No evidence patches were selected."
            )

        highlighted = {
            (
                str(item["render_mesh_snapshot_id"]),
                str(item["primitive_id"]),
                int(item["triangle_id"]),
            )
            for patch in patches
            for item in patch["triangle_refs"]
        }
        rows = projected.rows
        all_xy = list(projected.all_xy)
        if not all_xy:
            raise DFMError("render_scene_invalid", "The render scene is empty.")

        focus_values = [
            (
                patch,
                (
                    _dot(patch["focus_point"], view["basis_u"]),
                    _dot(patch["focus_point"], view["basis_v"]),
                ),
            )
            for patch in patches
        ]
        patch_xy = [
            (_dot(point, view["basis_u"]), _dot(point, view["basis_v"]))
            for patch in patches
            for point in _bounds_corners(patch["bounds"])
        ]
        scene_span = max(
            max(value[0] for value in all_xy) - min(value[0] for value in all_xy),
            max(value[1] for value in all_xy) - min(value[1] for value in all_xy),
            1.0,
        )

        def fit_transform(
            framing: list[tuple[float, float]],
            panel_width: int,
            panel_height: int,
            padding: int,
        ) -> tuple[float, float, float]:
            minimum_x = min(point[0] for point in framing)
            maximum_x = max(point[0] for point in framing)
            minimum_y = min(point[1] for point in framing)
            maximum_y = max(point[1] for point in framing)
            span_x = max(maximum_x - minimum_x, 1e-6)
            span_y = max(maximum_y - minimum_y, 1e-6)
            scale = min(
                (panel_width - 2 * padding) / span_x,
                (panel_height - 2 * padding) / span_y,
            )
            return scale, (minimum_x + maximum_x) / 2, (minimum_y + maximum_y) / 2

        def draw_mesh(
            panel_size: tuple[int, int],
            framing: list[tuple[float, float]],
            *,
            detail: bool,
            annotate: bool = False,
        ) -> Any:
            panel_width, panel_height = panel_size
            rendered = vertical_gradient(panel_size, panel_top, panel_bottom)
            rendered_draw = ImageDraw.Draw(rendered, "RGBA")
            scale, center_x, center_y = fit_transform(
                framing,
                panel_width,
                panel_height,
                (80 if detail else 30) * supersample,
            )

            def screen(point: tuple[float, float]) -> tuple[int, int]:
                return (
                    round(panel_width / 2 + (point[0] - center_x) * scale),
                    round(panel_height / 2 - (point[1] - center_y) * scale),
                )

            projected_rows = [
                (row, [screen(point) for point in row.projected]) for row in rows
            ]
            model_mask = Image.new("L", panel_size, 0)
            model_mask_draw = ImageDraw.Draw(model_mask)
            shadow_mask = Image.new("L", panel_size, 0)
            shadow_draw = ImageDraw.Draw(shadow_mask)
            for _row, polygon in projected_rows:
                model_mask_draw.polygon(polygon, fill=255)
                shadow_draw.polygon(
                    [(x + 8 * supersample, y + 12 * supersample) for x, y in polygon],
                    fill=92,
                )
            shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(16 * supersample))
            shadow_layer = Image.new("RGBA", panel_size, (45, 58, 76, 0))
            shadow_layer.putalpha(shadow_mask)
            rendered = Image.alpha_composite(
                rendered.convert("RGBA"), shadow_layer
            ).convert("RGB")
            rendered_draw = ImageDraw.Draw(rendered, "RGBA")

            light = _unit_or_none([-0.28, -0.48, 0.83])
            assert light is not None
            highlight_polygons = []
            for row, polygon in projected_rows:
                if not _visible(polygon, panel_width, panel_height):
                    continue
                intensity = 0.91 + 0.11 * abs(_dot(row.normal, light))
                shade = tuple(
                    min(255, round(channel * intensity)) for channel in (199, 208, 220)
                )
                rendered_draw.polygon(polygon, fill=(*shade, 255))
                if row.key in highlighted:
                    highlight_polygons.append(polygon)

            highlight_mask = Image.new("L", panel_size, 0)
            highlight_mask_draw = ImageDraw.Draw(highlight_mask)
            for polygon in highlight_polygons:
                rendered_draw.polygon(polygon, fill=(*accent, 112 if detail else 72))
                highlight_mask_draw.polygon(polygon, fill=255)

            kernel = 2 * supersample + 1
            silhouette = ImageChops.subtract(
                model_mask.filter(ImageFilter.MaxFilter(kernel)), model_mask
            ).point(lambda value: round(value * (0.58 if detail else 0.44)))
            rendered.paste((67, 82, 103), (0, 0), silhouette)
            highlight_edge = ImageChops.subtract(
                highlight_mask.filter(ImageFilter.MaxFilter(kernel)),
                highlight_mask.filter(ImageFilter.MinFilter(kernel)),
            ).point(lambda value: round(value * 0.68))
            rendered.paste(accent_dark, (0, 0), highlight_edge)
            rendered_draw = ImageDraw.Draw(rendered, "RGBA")

            radius = (6 if detail else 5) * supersample
            halo = (13 if detail else 10) * supersample
            markers = [(patch, screen(focus)) for patch, focus in focus_values]
            for _patch, marker in markers:
                rendered_draw.ellipse(
                    (
                        marker[0] - halo,
                        marker[1] - halo,
                        marker[0] + halo,
                        marker[1] + halo,
                    ),
                    fill=(255, 255, 255, 235),
                )
                rendered_draw.ellipse(
                    (
                        marker[0] - radius,
                        marker[1] - radius,
                        marker[0] + radius,
                        marker[1] + radius,
                    ),
                    fill=(*accent, 255),
                    outline=(255, 255, 255, 255),
                    width=2 * supersample,
                )

            if detail and annotate:
                callout_font = font(11 * supersample, bold=True)
                label_boxes: list[tuple[int, int, int, int]] = []
                for ordinal, (_patch, marker) in enumerate(markers, start=1):
                    marker_callout = f"ISSUE {ordinal:02d}"
                    text_box = rendered_draw.textbbox(
                        (0, 0), marker_callout, font=callout_font
                    )
                    callout_width = text_box[2] - text_box[0] + 28 * supersample
                    callout_height = 34 * supersample
                    to_right = marker[0] < panel_width * 0.68
                    label_left = (
                        marker[0] + 44 * supersample
                        if to_right
                        else marker[0] - callout_width - 44 * supersample
                    )
                    label_left = max(
                        22 * supersample,
                        min(
                            label_left,
                            panel_width - callout_width - 22 * supersample,
                        ),
                    )
                    base_top = marker[1] - 48 * supersample
                    candidates = [
                        base_top + offset * supersample
                        for offset in (0, -44, 44, -88, 88, -132, 132)
                    ]
                    label_top = max(
                        54 * supersample,
                        min(
                            candidates[0],
                            panel_height - callout_height - 36 * supersample,
                        ),
                    )
                    for candidate_top in candidates:
                        candidate_top = max(
                            54 * supersample,
                            min(
                                candidate_top,
                                panel_height - callout_height - 36 * supersample,
                            ),
                        )
                        candidate_box = (
                            label_left,
                            candidate_top,
                            label_left + callout_width,
                            candidate_top + callout_height,
                        )
                        if not any(
                            _rectangles_overlap(candidate_box, occupied)
                            for occupied in label_boxes
                        ):
                            label_top = candidate_top
                            break
                    label_box = (
                        label_left,
                        label_top,
                        label_left + callout_width,
                        label_top + callout_height,
                    )
                    label_boxes.append(label_box)
                    line_end = (
                        label_left if to_right else label_left + callout_width,
                        label_top + callout_height // 2,
                    )
                    elbow = (
                        marker[0] + (24 if to_right else -24) * supersample,
                        line_end[1],
                    )
                    rendered_draw.line(
                        (marker, elbow, line_end),
                        fill=(*accent_dark, 210),
                        width=supersample,
                    )
                    rendered_draw.rounded_rectangle(
                        label_box,
                        radius=17 * supersample,
                        fill=(255, 255, 255, 242),
                        outline=(*accent, 100),
                        width=supersample,
                    )
                    rendered_draw.ellipse(
                        (
                            label_left + 11 * supersample,
                            label_top + 14 * supersample,
                            label_left + 17 * supersample,
                            label_top + 20 * supersample,
                        ),
                        fill=(*accent, 255),
                    )
                    rendered_draw.text(
                        (
                            label_left + 23 * supersample,
                            label_top + 9 * supersample,
                        ),
                        marker_callout,
                        font=callout_font,
                        fill=ink,
                    )
            return rendered

        field_refs = {str(patch.get("field_ref") or "").lower() for patch in patches}
        if field_refs and all("draft" in field_ref for field_ref in field_refs):
            title = "DRAFT ANGLE"
        elif field_refs and all("thickness" in field_ref for field_ref in field_refs):
            title = "WALL THICKNESS"
        elif field_refs and all("radius" in field_ref for field_ref in field_refs):
            title = "RADIUS CHECK"
        elif len(field_refs) > 1:
            title = "DFM EVIDENCE"
        else:
            title = "GEOMETRY CHECK"

        detail_framing = list(patch_xy)
        patch_span_x = max(item[0] for item in patch_xy) - min(
            item[0] for item in patch_xy
        )
        patch_span_y = max(item[1] for item in patch_xy) - min(
            item[1] for item in patch_xy
        )
        minimum_span = scene_span * 0.12
        focus_center = (
            sum(focus[0] for _patch, focus in focus_values) / len(focus_values),
            sum(focus[1] for _patch, focus in focus_values) / len(focus_values),
        )
        if patch_span_x < minimum_span or patch_span_y < minimum_span:
            half = minimum_span / 2
            detail_framing.extend([
                (focus_center[0] - half, focus_center[1] - half),
                (focus_center[0] + half, focus_center[1] + half),
            ])

        canvas = vertical_gradient((width, height), background_top, background_bottom)
        context = draw_mesh(
            (330 * supersample, 205 * supersample), all_xy, detail=False
        )
        margin = 32 * supersample
        header_height = 108 * supersample
        hero_width = width - margin * 2
        hero_height = height - header_height - margin
        detail = draw_mesh(
            (hero_width, hero_height),
            detail_framing,
            detail=True,
            annotate=True,
        )
        hero_box = (margin, header_height, width - margin, height - margin)
        paste_rounded(
            canvas,
            detail,
            hero_box,
            radius=22 * supersample,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.rounded_rectangle(
            hero_box,
            radius=22 * supersample,
            outline=(*hairline, 210),
            width=supersample,
        )

        inset_left = margin + 22 * supersample
        inset_bottom = height - margin - 22 * supersample
        inset_box = (
            inset_left,
            inset_bottom - 205 * supersample,
            inset_left + 330 * supersample,
            inset_bottom,
        )
        paste_rounded(
            canvas,
            context,
            inset_box,
            radius=16 * supersample,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.rounded_rectangle(
            inset_box,
            radius=16 * supersample,
            outline=(255, 255, 255, 210),
            width=2 * supersample,
        )

        eyebrow_font = font(10 * supersample, bold=True)
        title_font = font(26 * supersample, bold=True)
        small_font = font(11 * supersample)
        badge_font = font(11 * supersample, bold=True)
        draw.rounded_rectangle(
            (margin, 25 * supersample, margin + 4 * supersample, 80 * supersample),
            radius=2 * supersample,
            fill=accent,
        )
        draw.text(
            (margin + 18 * supersample, 21 * supersample),
            "HERMES  /  DESIGN FOR MANUFACTURING",
            font=eyebrow_font,
            fill=muted,
        )
        draw.text(
            (margin + 18 * supersample, 41 * supersample),
            title,
            font=title_font,
            fill=ink,
        )
        draw.text(
            (margin + 18 * supersample, 77 * supersample),
            (
                f"Evidence {len(patches)} region{'s' if len(patches) != 1 else ''}"
                f"  ·  {len(prepared.triangles)} triangles  ·  deterministic trace"
            ),
            font=small_font,
            fill=muted,
        )

        label = str(view["label"]).upper()
        label = (
            f"{label} · {str(view.get('presentation_mode', 'shared')).upper()} · X-RAY"
        )
        label_box = draw.textbbox((0, 0), label, font=badge_font)
        label_width = label_box[2] - label_box[0] + 28 * supersample
        badge_left = width - margin - label_width
        draw.rounded_rectangle(
            (badge_left, 31 * supersample, width - margin, 65 * supersample),
            radius=17 * supersample,
            fill=ink,
        )
        draw.text(
            (badge_left + 14 * supersample, 40 * supersample),
            label,
            font=badge_font,
            fill=(255, 255, 255),
        )

        status = "ACTION REQUIRED"
        status_box = draw.textbbox((0, 0), status, font=badge_font)
        status_width = status_box[2] - status_box[0] + 42 * supersample
        status_left = badge_left - status_width - 10 * supersample
        draw.rounded_rectangle(
            (
                status_left,
                31 * supersample,
                status_left + status_width,
                65 * supersample,
            ),
            radius=17 * supersample,
            fill=accent_pale,
        )
        draw.ellipse(
            (
                status_left + 13 * supersample,
                45 * supersample,
                status_left + 19 * supersample,
                51 * supersample,
            ),
            fill=accent,
        )
        draw.text(
            (status_left + 26 * supersample, 40 * supersample),
            status,
            font=badge_font,
            fill=accent_dark,
        )

        inset_label_left = inset_left + 13 * supersample
        inset_label_top = inset_box[1] + 13 * supersample
        draw.rounded_rectangle(
            (
                inset_label_left,
                inset_label_top,
                inset_label_left + 98 * supersample,
                inset_label_top + 28 * supersample,
            ),
            radius=14 * supersample,
            fill=(255, 255, 255, 225),
        )
        draw.text(
            (inset_label_left + 13 * supersample, inset_label_top + 7 * supersample),
            "PART CONTEXT",
            font=eyebrow_font,
            fill=muted,
        )

        legend_y = height - margin - 38 * supersample
        cursor = width - margin - 22 * supersample
        legend_font = font(10 * supersample, bold=True)
        for legend_label, color in (
            ("PROBLEM REGIONS", accent),
            ("EVALUATED SURFACE", (224, 118, 111)),
            ("MODEL", (157, 170, 188)),
        ):
            box = draw.textbbox((0, 0), legend_label, font=legend_font)
            item_width = box[2] - box[0] + 24 * supersample
            cursor -= item_width
            draw.ellipse(
                (
                    cursor,
                    legend_y + 5 * supersample,
                    cursor + 8 * supersample,
                    legend_y + 13 * supersample,
                ),
                fill=color,
            )
            draw.text(
                (cursor + 14 * supersample, legend_y),
                legend_label,
                font=legend_font,
                fill=(69, 84, 105),
            )
            cursor -= 16 * supersample

        canvas.resize(final_size, Image.Resampling.LANCZOS).save(target, format="PNG")


def _coverage_requirements(patches: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the geometry that the evidence image set is required to cover."""

    required_patch_ids = sorted(str(patch["patch_id"]) for patch in patches)
    failed_sample_refs = sorted({
        str(sample_ref)
        for patch in patches
        if patch.get("coverage_mode") == "failed_samples"
        for sample_ref in patch.get("sample_refs", [])
    })
    contributing_patch_ids = sorted(
        str(patch["patch_id"])
        for patch in patches
        if patch.get("coverage_mode") == "contributing_regions"
    )
    return {
        "required_patch_ids": required_patch_ids,
        "failed_sample_refs": failed_sample_refs,
        "contributing_patch_ids": contributing_patch_ids,
    }


def _coverage_summary(
    patches: list[dict[str, Any]], covered_patch_ids: set[str]
) -> dict[str, Any]:
    """Prove that the generated image set covers every required patch and sample."""

    requirements = _coverage_requirements(patches)
    required_patch_ids = set(requirements["required_patch_ids"])
    covered = required_patch_ids.intersection(covered_patch_ids)
    sample_refs_by_patch = {
        str(patch["patch_id"]): {
            str(sample_ref)
            for sample_ref in patch.get("sample_refs", [])
            if patch.get("coverage_mode") == "failed_samples"
        }
        for patch in patches
    }
    covered_sample_refs = {
        sample_ref
        for patch_id in covered
        for sample_ref in sample_refs_by_patch[patch_id]
    }
    required_sample_refs = set(requirements["failed_sample_refs"])
    required_count = len(required_patch_ids)
    covered_count = len(covered)
    return {
        "required_patch_count": required_count,
        "covered_patch_count": covered_count,
        "coverage_ratio": covered_count / required_count if required_count else 1.0,
        "covered_patch_ids": sorted(covered),
        "uncovered_patch_ids": sorted(required_patch_ids - covered),
        "required_sample_count": len(required_sample_refs),
        "covered_sample_count": len(covered_sample_refs),
        "uncovered_sample_refs": sorted(required_sample_refs - covered_sample_refs),
    }


def _field_pull_direction(
    project_dir: Path,
    artifacts: list[ArtifactRecord],
    payload_cache: dict[str, dict[str, Any]] | None = None,
) -> list[float] | None:
    for artifact in artifacts:
        if artifact.kind != "scalar_field":
            continue
        field = _read_json(project_dir, artifact, payload_cache)
        context = field.get("calculation_context")
        if not isinstance(context, dict):
            continue
        direction = _unit_or_none(context.get("pull_direction"))
        if direction is not None:
            return direction
    return None


def _average_direction(values: list[Any]) -> list[float] | None:
    directions = [item for value in values if (item := _unit_or_none(value))]
    if not directions:
        return None
    reference = directions[0]
    aligned = [
        direction if _dot(direction, reference) >= 0 else [-item for item in direction]
        for direction in directions
    ]
    return _unit_or_none([
        sum(direction[axis] for direction in aligned) for axis in range(3)
    ])


def _unit_or_none(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    length = math.sqrt(sum(item * item for item in vector))
    if length <= 1e-12:
        return None
    return [item / length for item in vector]


def _bounds_corners(bounds: dict[str, Any]) -> list[list[float]]:
    minimum = bounds["minimum"]
    maximum = bounds["maximum"]
    return [
        [x, y, z]
        for x in (minimum[0], maximum[0])
        for y in (minimum[1], maximum[1])
        for z in (minimum[2], maximum[2])
    ]


def _read_json(
    project_dir: Path,
    artifact: ArtifactRecord,
    cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if cache is not None and artifact.artifact_id in cache:
        return cache[artifact.artifact_id]
    try:
        payload = json.loads(
            (project_dir / artifact.relative_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise DFMError(
            "evidence_artifact_invalid",
            f"The {artifact.kind} artifact is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise DFMError(
            "evidence_artifact_invalid",
            f"The {artifact.kind} artifact must be a JSON object.",
        )
    if cache is not None:
        cache[artifact.artifact_id] = payload
    return payload


def _connected_cells(
    cells: list[dict[str, Any]], scene: dict[str, Any]
) -> list[list[dict[str, Any]]]:
    """Return connected failed cells using indexed near-linear union operations."""

    triangle_vertices = {
        (
            str(primitive.get("render_mesh_snapshot_id")),
            str(primitive.get("primitive_id")),
            triangle_id,
        ): {
            tuple(round(float(value), 9) for value in vertices[int(vertex_id)])
            for vertex_id in triangle
        }
        for primitive in scene.get("primitives", [])
        for vertices in [primitive.get("vertices", [])]
        for triangle_id, triangle in enumerate(primitive.get("triangles", []))
    }

    def mesh_vertices(cell: dict[str, Any]) -> set[tuple[float, ...]]:
        ref = cell.get("triangle_ref", {})
        return triangle_vertices.get(
            (
                str(ref.get("render_mesh_snapshot_id")),
                str(ref.get("primitive_id")),
                int(ref.get("triangle_id", -1)),
            ),
            set(),
        )

    parents = list(range(len(cells)))
    ranks = [0] * len(cells)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if ranks[left_root] < ranks[right_root]:
            left_root, right_root = right_root, left_root
        parents[right_root] = left_root
        if ranks[left_root] == ranks[right_root]:
            ranks[left_root] += 1

    sample_owner: dict[str, int] = {}
    edge_owner: dict[tuple[tuple[float, ...], tuple[float, ...]], int] = {}
    for cell_index, cell in enumerate(cells):
        for sample_id in {str(item) for item in cell.get("sample_ids", [])}:
            owner = sample_owner.setdefault(sample_id, cell_index)
            union(cell_index, owner)

        vertices = sorted(mesh_vertices(cell))
        for left_index in range(len(vertices)):
            for right_index in range(left_index + 1, len(vertices)):
                edge = (vertices[left_index], vertices[right_index])
                owner = edge_owner.setdefault(edge, cell_index)
                union(cell_index, owner)

    grouped: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for cell_index, cell in enumerate(cells):
        grouped.setdefault(find(cell_index), []).append((cell_index, cell))
    ordered = sorted(grouped.values(), key=lambda group: group[0][0])
    return [[cell for _index, cell in group] for group in ordered]


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"))
        unique[key] = value
    return list(unique.values())


def _stable_content_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _dot(left: list[float], right: tuple[float, float, float]) -> float:
    return sum(float(left[index]) * right[index] for index in range(3))


def _visible(points: list[tuple[int, int]], width: int, height: int) -> bool:
    return not (
        max(point[0] for point in points) < 0
        or min(point[0] for point in points) >= width
        or max(point[1] for point in points) < 0
        or min(point[1] for point in points) >= height
    )


def _rectangles_overlap(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    return not (
        left[2] <= right[0]
        or left[0] >= right[2]
        or left[3] <= right[1]
        or left[1] >= right[3]
    )
