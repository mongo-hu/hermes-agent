"""Fast, dependency-free STEP intake checks run before the OCC worker."""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import DFMError


_ENTITY = re.compile(r"(?m)^\s*(#\d+)\s*=")
_B_REP = re.compile(
    r"\b(?:MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS|ADVANCED_BREP_SHAPE_REPRESENTATION)\b",
    re.IGNORECASE,
)
_STEP_START = b"ISO-10303-21;"
_STEP_END = b"END-ISO-10303-21;"


def inspect_step(path: Path) -> dict[str, object]:
    """Validate a physical STEP exchange file without loading CAD libraries.

    This is deliberately a bounded lexical preflight, not a geometry analysis.
    OpenCascade remains the authority for full B-Rep loading in the isolated
    worker.  Rejecting malformed or non-B-Rep inputs here prevents expensive
    worker startup for inputs that cannot be analysed by this product scope.
    """

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DFMError("input_unreadable", "STEP input could not be read.") from exc

    stripped = payload.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if not stripped.startswith(_STEP_START) or _STEP_END not in stripped:
        raise DFMError(
            "step_format_invalid",
            "The STEP input is not an ISO 10303-21 exchange file.",
        )
    try:
        text = payload.decode("latin-1")
    except UnicodeDecodeError as exc:  # latin-1 is total; keep an explicit contract.
        raise DFMError("step_format_invalid", "The STEP input is not readable text.") from exc

    upper = text.upper()
    if "HEADER;" not in upper or "DATA;" not in upper or "ENDSEC;" not in upper:
        raise DFMError(
            "step_format_invalid",
            "The STEP input is missing required HEADER or DATA sections.",
        )
    if not _B_REP.search(text):
        raise DFMError(
            "step_brep_unreadable",
            "The STEP input does not declare a supported B-Rep representation.",
        )

    entities = _ENTITY.findall(text)
    if not entities:
        raise DFMError(
            "step_brep_unreadable",
            "The STEP input contains no readable B-Rep entities.",
        )
    return {
        "status": "passed",
        "format": "iso-10303-21",
        "brep_representation": "declared",
        "entity_count": len(entities),
        "complexity": {"entity_count": len(entities)},
    }
