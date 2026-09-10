import pytest

from tools.dfm.contracts import BoundingBox, GeometryRef, PlanOperation, RegionRecord, ResolvedArgument
from tools.dfm.evidence.field_engine import _connected_cells
from tools.dfm.geometry.step import field_export


def test_curved_draft_field_uses_each_uv_sample_local_normal(monkeypatch):
    operation = PlanOperation(
        "geometry.draft",
        "measure_draft",
        metric_ids=["injection.geometry.draft"],
        required_quantities=["draft_angle_deg"],
        required_artifacts=["scalar_field", "render_scene", "topology_map"],
        arguments={
            "pull_direction": ResolvedArgument([0, 0, 1], "fact:pull_dir")
        },
    )
    mesh = [{
        "face": object(),
        "location": object(),
        "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 1]],
        "uvs": [[0, 0], [1, 0], [0, 1]],
            "primitive": {
                "primitive_id": "face-1",
                "render_mesh_snapshot_id": "mesh_test",
            "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 1]],
            "triangles": [[0, 1, 2]],
        },
            "geometry_ref": {"kind": "face", "index": 1, "input_sha256": "a" * 64, "topology_snapshot_id": "topology_test", "entity_id": "face_000001"},
    }]
    normals = {
        (0, 0): [1, 0, 0],
        (1, 0): [0.5, 0, 0.8660254038],
        (0, 1): [0, 0, 1],
    }
    monkeypatch.setattr(
        field_export,
        "_surface_normal",
        lambda _item, uv: normals[tuple(uv)],
    )

    field, measurement = field_export._draft_field(
        mesh,
        operation,
        "run_1",
        "a" * 64,
        "field_draft",
        [0, 0, 1],
    )

    assert [sample["value"] for sample in field["samples"]] == pytest.approx(
        [0, 60, 90]
    )
    assert measurement.value == pytest.approx(0)
    assert measurement.field_refs == ["field_draft"]
    assert measurement.quality["backend"] == "pythonocc_demo"
    assert measurement.quality["certified"] is False
    assert field["calculation_context"] == {"pull_direction": [0.0, 0.0, 1.0]}
    assert "evaluation_hint" not in measurement.diagnostics


def test_thickness_contract_allows_cell_center_without_mesh_vertex():
    operation = PlanOperation(
        "geometry.wall_thickness",
        "measure_wall_thickness",
        metric_ids=["injection.geometry.wall_thickness"],
        required_quantities=["thickness_mm"],
    )
    sample = {
        "sample_id": "thickness-face-1-t0",
        "point": [0.3, 0.3, 0],
        "uv": None,
        "surface_normal": [0, 0, 1],
        "value": 0.8,
        "geometry_ref": {"kind": "face", "index": 1, "input_sha256": "a" * 64, "topology_snapshot_id": "topology_test", "entity_id": "face_000001"},
        "mesh_vertex_ref": None,
    }

    field, measurement = field_export._field_and_measurement(
        operation,
        "run_1",
        "a" * 64,
        "field_wall_thickness",
        "thickness_mm",
        "mm",
        "constant_per_triangle",
        [sample],
        [{
            "cell_id": sample["sample_id"],
            "sample_ids": [sample["sample_id"]],
            "geometry_ref": sample["geometry_ref"],
            "triangle_ref": {"primitive_id": "face-1", "triangle_id": 0, "render_mesh_snapshot_id": "mesh_test"},
        }],
        [0.8],
        {},
    )

    assert field["samples"][0]["mesh_vertex_ref"] is None
    assert field["interpolation"] == "constant_per_triangle"
    assert field["calculation_context"] == {}
    assert measurement.value == 0.8


def test_adjacent_cell_center_thickness_samples_form_one_patch():
    scene = {
        "primitives": [{
            "primitive_id": "face-1",
            "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
            "triangles": [[0, 1, 2], [0, 2, 3]],
        }]
    }
    cells = [
        {
            "sample_ids": ["center-1"],
            "triangle_ref": {"primitive_id": "face-1", "triangle_id": 0},
        },
        {
            "sample_ids": ["center-2"],
            "triangle_ref": {"primitive_id": "face-1", "triangle_id": 1},
        },
    ]

    assert _connected_cells(cells, scene) == [cells[::-1]]


def test_region_selector_splits_feature_faces_from_ordinary_complement():
    digest = "a" * 64
    mesh = [
        {"geometry_ref": {"kind": "face", "index": index, "input_sha256": digest}}
        for index in (1, 2, 3)
    ]
    feature_region = RegionRecord(
        "region.feature.wall", digest, "model", "topology_refs", "feature wall",
        ["recognizer:test"], "1", "b" * 64,
        geometry_refs=[GeometryRef("face", 2, digest, "topology_test", "face_000002")], role="wall",
        feature_refs=["feature.1"],
    )
    ordinary_region = RegionRecord(
        "region.ordinary", digest, "model", "topology_complement", "ordinary",
        ["recognizer:fallback"], "1", "c" * 64,
        excluded_geometry_refs=[GeometryRef("face", 2, digest, "topology_test", "face_000002")], role="ordinary",
        feature_refs=["feature.ordinary"],
    )

    feature_mesh = field_export._operation_mesh(
        mesh,
        PlanOperation("wall.feature", "measure_wall_thickness", region_refs=[feature_region.region_id]),
        {feature_region.region_id: feature_region}, digest,
    )
    ordinary_mesh = field_export._operation_mesh(
        mesh,
        PlanOperation("wall.ordinary", "measure_wall_thickness", region_refs=[ordinary_region.region_id]),
        {ordinary_region.region_id: ordinary_region}, digest,
    )

    assert [item["geometry_ref"]["index"] for item in feature_mesh] == [2]
    assert [item["geometry_ref"]["index"] for item in ordinary_mesh] == [1, 3]


def test_bbox_region_preserves_original_triangle_ids_for_evidence():
    digest = "a" * 64
    mesh = [{
        "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [10, 10, 0]],
        "primitive": {"primitive_id": "face-1", "triangles": [[0, 1, 2], [1, 2, 3]]},
        "triangle_ids": [7, 9],
        "geometry_ref": {"kind": "face", "index": 1, "input_sha256": digest},
    }]
    region = RegionRecord(
        "region.bbox", digest, "model", "bbox", "local", ["user"], "1", "b" * 64,
        bbox=BoundingBox([-1, -1, -1], [2, 2, 1]), role="ordinary",
        feature_refs=["feature.ordinary"],
    )

    selected = field_export._operation_mesh(
        mesh,
        PlanOperation("draft.local", "measure_draft", region_refs=[region.region_id]),
        {region.region_id: region}, digest,
    )

    assert selected[0]["primitive"]["triangles"] == [[0, 1, 2]]
    assert selected[0]["triangle_ids"] == [7]
