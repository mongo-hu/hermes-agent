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
class DrawingFragment:
    text: str
    confidence: float = 0.0
    page: int | None = None
    bbox: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DrawingPipelineResult:
    provider: str
    provider_version: str
    fragments: list[DrawingFragment] = field(default_factory=list)
    raw_text: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "fragments": [item.to_dict() for item in self.fragments],
            "diagnostics": dict(self.diagnostics),
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


def execute_2d_pipeline(
    file_path: str,
    *,
    max_pages: int = 50,
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
    fragments = []
    for item in payload.get("fragments") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        fragments.append(
            DrawingFragment(
                text=text,
                confidence=max(0.0, min(float(item.get("confidence") or 0.0), 1.0)),
                page=item.get("page"),
                bbox=item.get("bbox"),
            )
        )
    return DrawingPipelineResult(
        provider="hermes_drawing_pipeline",
        provider_version=DRAWING_PIPELINE_VERSION,
        fragments=fragments,
        raw_text=raw_text,
        diagnostics=diagnostics,
    )
