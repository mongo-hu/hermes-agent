"""Lightweight Parasolid XT intake checks; this module does not parse B-Rep."""

from __future__ import annotations

from pathlib import Path

from ..errors import DFMError


def inspect_parasolid_xt(path: Path) -> dict[str, object]:
    """Validate enough text structure to register x_t as an opaque input.

    Real Parasolid version, units, bodies, topology and geometry capabilities
    are determined only by the remote NX backend.
    """

    if path.suffix.lower() != ".x_t":
        raise DFMError(
            "input_format_invalid",
            "Parasolid text input must use the .x_t extension.",
        )
    prefix = path.read_bytes()[:4096]
    if not prefix or b"\x00" in prefix:
        raise DFMError(
            "input_format_invalid",
            "The x_t input is empty or appears to be a binary Parasolid file.",
        )
    try:
        header = prefix.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DFMError(
            "input_format_invalid",
            "The x_t input header is not valid ASCII text.",
        ) from exc
    return {
        "format_id": "parasolid_xt",
        "representation_claim": "brep",
        "header_preview": header.splitlines()[0][:160],
        "inspection_level": "opaque_text_only",
        "geometry_verified": False,
        "geometry_verifier": "external_backend_required",
    }
