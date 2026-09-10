import pytest

from tools.dfm.geometry.snapshot_hash import render_mesh_content_sha256


def test_render_mesh_hash_has_a_cross_language_contract_vector():
    primitives = [
        {
            "primitive_id": "face-7",
            "render_mesh_snapshot_id": "ignored",
            "vertices": [
                [-0.0, 1.5, -2.25],
                [3.0, 4.125, 5.0],
                [6.5, 7.0, 8.25],
            ],
            "triangles": [[0, 1, 2]],
        }
    ]

    assert render_mesh_content_sha256(primitives) == (
        "4df80ada7123cc605e2d77f35ad73650408a9b5d8e25cac052fee6694ee03ef5"
    )


def test_render_mesh_hash_excludes_snapshot_identity_and_normalizes_negative_zero():
    first = [
        {
            "primitive_id": "face-1",
            "render_mesh_snapshot_id": "mesh_first",
            "vertices": [[-0.0, 0, 0], [1, 0, 0], [0, 1, 0]],
            "triangles": [[0, 1, 2]],
        }
    ]
    second = [
        {
            **first[0],
            "render_mesh_snapshot_id": "mesh_second",
            "vertices": [[0.0, 0, 0], [1, 0, 0], [0, 1, 0]],
        }
    ]

    assert render_mesh_content_sha256(first) == render_mesh_content_sha256(second)


def test_render_mesh_hash_rejects_non_finite_coordinates():
    with pytest.raises(ValueError, match="finite numbers"):
        render_mesh_content_sha256(
            [
                {
                    "primitive_id": "face-1",
                    "vertices": [[float("nan"), 0, 0], [1, 0, 0], [0, 1, 0]],
                    "triangles": [[0, 1, 2]],
                }
            ]
        )


def test_render_mesh_hash_rejects_json_string_coordinates():
    with pytest.raises(ValueError, match="finite numbers"):
        render_mesh_content_sha256(
            [
                {
                    "primitive_id": "face-1",
                    "vertices": [["0.0", 0, 0], [1, 0, 0], [0, 1, 0]],
                    "triangles": [[0, 1, 2]],
                }
            ]
        )
