"""Stable boundary for the isolated DFM drawing pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .core_pipeline import dependency_report, process_file


DRAWING_PIPELINE_VERSION = "2.0.0"
SUPPORTED_DRAWING_SUFFIXES = (".jpeg", ".jpg", ".pdf", ".png")


class DrawingPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class DrawingCandidate:
    kind: str
    value: Any
    unit: str | None = None
    confidence: float = 0.0
    page: int | None = None
    bbox: list[float] | None = None
    original_text: str = ""
    feature_kind: str = ""
    region_role: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DrawingPipelineResult:
    provider: str
    provider_version: str
    candidates: list[DrawingCandidate] = field(default_factory=list)
    raw_text: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "candidates": [item.to_dict() for item in self.candidates],
            "diagnostics": dict(self.diagnostics),
        }


_FIELDS: dict[str, tuple[str, str | None, bool]] = {
    "material": ("material", None, False),
    "general_tolerance": ("general_tolerance", None, False),
    "surface_finish": ("surface_finish", None, False),
    "part_name": ("part_name", None, False),
    "manufacturing_constraints": ("manufacturing_constraint", None, True),
    "thread_requirements": ("thread_requirement", None, True),
    "other_global_notes": ("global_note", None, True),
}


def pipeline_capability(suffixes: set[str] | None = None) -> dict[str, Any]:
    requested = {item.lower() for item in suffixes or set()}
    unsupported = sorted(requested - set(SUPPORTED_DRAWING_SUFFIXES))
    report = dependency_report(requested)
    return {
        **report,
        "available": bool(report["available"] and not unsupported),
        "unsupported_formats": unsupported,
        "supported_formats": list(SUPPORTED_DRAWING_SUFFIXES),
        "provider_version": DRAWING_PIPELINE_VERSION,
    }


def _source_fragment(
    value: Any, fragments: list[dict[str, Any]]
) -> dict[str, Any] | None:
    needle = str(value or "").strip().casefold()
    if not needle:
        return None
    return next(
        (
            item
            for item in fragments
            if needle in str(item.get("text") or "").casefold()
        ),
        None,
    )


def _candidates(payload: dict[str, Any]) -> list[DrawingCandidate]:
    extraction = payload.get("extraction") or {}
    fragments = list(payload.get("fragments") or [])
    output: list[DrawingCandidate] = []
    for field_name, (kind, unit, repeated) in _FIELDS.items():
        raw_value = extraction.get(field_name)
        values = raw_value if repeated and isinstance(raw_value, list) else [raw_value]
        for value in values:
            if value is None or str(value).strip().lower() in {
                "",
                "null",
                "none",
                "unknown",
            }:
                continue
            if kind == "material" and isinstance(value, str):
                value = value.strip().upper()
            fragment = _source_fragment(value, fragments)
            fragment_confidence = float((fragment or {}).get("confidence") or 0.0)
            output.append(
                DrawingCandidate(
                    kind=kind,
                    value=value,
                    unit=unit,
                    confidence=max(0.75, min(fragment_confidence, 0.98)),
                    page=(fragment or {}).get("page"),
                    bbox=(fragment or {}).get("bbox"),
                    original_text=str((fragment or {}).get("text") or "")[:500],
                )
            )
    return output


def execute_2d_pipeline(
    file_path: str,
    *,
    max_pages: int = 50,
    model_name: str = "",
    base_url: str = "",
    timeout_seconds: int = 60,
    processor: Callable[..., tuple[dict[str, Any], str]] = process_file,
) -> DrawingPipelineResult:
    path = Path(file_path)
    if not path.is_file():
        raise DrawingPipelineError(
            "drawing_input_missing",
            f"Drawing input does not exist: {path}",
            {"path": str(path)},
        )
    capability = pipeline_capability({path.suffix.lower()})
    if capability["unsupported_formats"]:
        raise DrawingPipelineError(
            "drawing_format_unsupported",
            f"Unsupported drawing format: {path.suffix.lower()}",
            capability,
        )
    if not capability["available"]:
        raise DrawingPipelineError(
            "drawing_dependency_missing",
            "Drawing analysis dependencies are unavailable.",
            capability,
        )
    try:
        payload, raw_text = processor(
            str(path),
            max_pages=max_pages,
            model_name=model_name,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
    except DrawingPipelineError:
        raise
    except Exception as exc:
        raise DrawingPipelineError(
            "drawing_pipeline_failed",
            f"Drawing pipeline failed: {exc}",
            {"error_type": type(exc).__name__},
        ) from exc
    diagnostics = dict(payload.get("diagnostics") or {})
    semantic = diagnostics.get("semantic_extraction") or {}
    if semantic.get("status") == "failed":
        diagnostics["warnings"] = [
            {
                "code": "drawing_semantic_extraction_failed",
                "message": semantic.get("message") or "Semantic extraction failed.",
            }
        ]
    return DrawingPipelineResult(
        provider="hermes_drawing_pipeline",
        provider_version=DRAWING_PIPELINE_VERSION,
        candidates=_candidates(payload),
        raw_text=raw_text,
        diagnostics=diagnostics,
    )
