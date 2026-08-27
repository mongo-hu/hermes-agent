"""Language-neutral content identities for shared geometry snapshots."""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Any


_RENDER_MESH_DOMAIN = b"dfm.render-mesh-snapshot/v2\0"
_MAX_UINT64 = (1 << 64) - 1


def render_mesh_content_sha256(primitives: list[dict[str, Any]]) -> str:
    """Hash render primitives without depending on a JSON float serializer.

    The byte stream is domain-separated and length-prefixed. Coordinates use
    canonical IEEE-754 binary64 big-endian bytes; counts and triangle indices
    use unsigned 64-bit big-endian integers. Negative zero is normalized to
    positive zero. ``render_mesh_snapshot_id`` is deliberately excluded.
    """

    digest = hashlib.sha256()
    digest.update(_RENDER_MESH_DOMAIN)
    _update_uint64(digest, len(primitives), "primitive count")
    for primitive in primitives:
        if not isinstance(primitive, dict):
            raise ValueError("Render primitives must be objects.")
        primitive_id = primitive.get("primitive_id")
        if not isinstance(primitive_id, str) or not primitive_id:
            raise ValueError("A render primitive has no identity.")
        _update_text(digest, primitive_id)

        vertices = primitive.get("vertices")
        if not isinstance(vertices, list):
            raise ValueError("Render primitive vertices must be an array.")
        _update_uint64(digest, len(vertices), "vertex count")
        for vertex in vertices:
            if not isinstance(vertex, list) or len(vertex) != 3:
                raise ValueError("Render vertices must contain three coordinates.")
            for coordinate in vertex:
                if isinstance(coordinate, bool) or not isinstance(
                    coordinate, (int, float)
                ):
                    raise ValueError("Render coordinates must be finite numbers.")
                value = float(coordinate)
                if not math.isfinite(value):
                    raise ValueError("Render coordinates must be finite numbers.")
                digest.update(struct.pack(">d", 0.0 if value == 0.0 else value))

        triangles = primitive.get("triangles")
        if not isinstance(triangles, list):
            raise ValueError("Render primitive triangles must be an array.")
        _update_uint64(digest, len(triangles), "triangle count")
        for triangle in triangles:
            if not isinstance(triangle, list) or len(triangle) != 3:
                raise ValueError("Render triangles must contain three vertex indices.")
            for index in triangle:
                if isinstance(index, bool) or not isinstance(index, int):
                    raise ValueError("Render triangle indices must be integers.")
                _update_uint64(digest, index, "triangle index")
    return digest.hexdigest()


def _update_text(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8")
    _update_uint64(digest, len(encoded), "text length")
    digest.update(encoded)


def _update_uint64(digest: Any, value: int, label: str) -> None:
    if value < 0 or value > _MAX_UINT64:
        raise ValueError(f"Render mesh {label} is outside uint64 range.")
    digest.update(struct.pack(">Q", value))
