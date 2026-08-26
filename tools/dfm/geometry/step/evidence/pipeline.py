"""Render evidence from completed checks without participating in measurement."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import rendering


@dataclass(frozen=True)
class EvidenceResult:
    rendered_findings: int
    highlighted_step: dict[str, Any] | None
    highlighted_step_error: str | None


def render_evidence_bundle(
    shape: Any,
    occ: SimpleNamespace,
    issues: list[Any],
    out_dir: Path,
    args: Any,
) -> EvidenceResult:
    # Lazy import preserves the worker's heavy-dependency boundary while the
    # migrated render implementation is extracted incrementally.
    from .. import legacy_analyzer as legacy

    rendered = rendering.render_issue_evidence(shape, occ, issues, out_dir, args)
    legacy.emit_dfm_event("progress", stage="render_overview", percent=80)
    rendering.render_outputs(shape, occ, issues, out_dir, args)
    highlighted = None
    error = None
    if args.highlight_step:
        legacy.emit_dfm_event("progress", stage="export_highlighted_step", percent=88)
        try:
            highlighted = rendering.export_highlighted_step(
                shape, occ, issues, out_dir / args.highlight_step_name
            )
        except Exception as exc:
            error = str(exc)
            print(
                f"[warn] highlighted STEP export failed: {exc}", file=legacy.sys.stderr
            )
    return EvidenceResult(rendered, highlighted, error)
