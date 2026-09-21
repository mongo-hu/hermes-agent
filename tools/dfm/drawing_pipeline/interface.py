"""Stable boundary for the isolated DFM drawing pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .core_pipeline import (
    dependency_report,
    process_file,
    render_crops as render_crop_images,
    render_overviews as render_page_images,
)


DRAWING_PIPELINE_VERSION = "3.0.0"
SUPPORTED_DRAWING_SUFFIXES = (".jpeg", ".jpg", ".pdf", ".png")


class DrawingPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class DrawingPage:
    page: int
    width: float
    height: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DrawingPipelineResult:
    provider: str
    provider_version: str
    pages: list[DrawingPage] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "pages": [item.to_dict() for item in self.pages],
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class DrawingImage:
    page: int
    pixel_width: int
    pixel_height: int
    png_bytes: bytes


@dataclass(frozen=True)
class DrawingCrop(DrawingImage):
    region_id: str
    type: str
    bbox_1000: list[int]
    decision: str
    reason: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "page": self.page,
            "type": self.type,
            "bbox_1000": list(self.bbox_1000),
            "decision": self.decision,
            "reason": self.reason,
            "pixel_width": self.pixel_width,
            "pixel_height": self.pixel_height,
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
    processor: Callable[..., dict[str, Any]] = process_file,
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
        payload = processor(
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
    pages = [
        DrawingPage(
            page=int(item["page"]),
            width=float(item["width"]),
            height=float(item["height"]),
        )
        for item in payload.get("pages") or []
    ]
    return DrawingPipelineResult(
        provider="hermes_semantic_crop_pipeline",
        provider_version=DRAWING_PIPELINE_VERSION,
        pages=pages,
        diagnostics=diagnostics,
    )


def render_overviews(
    file_path: str,
    *,
    max_pages: int = 50,
    renderer: Callable[..., list[dict[str, Any]]] = render_page_images,
) -> list[DrawingImage]:
    return [DrawingImage(**item) for item in renderer(file_path, max_pages=max_pages)]


def prepare_crops(
    file_path: str,
    regions: object,
    *,
    max_pages: int = 50,
    renderer: Callable[..., list[dict[str, Any]]] = render_crop_images,
) -> list[DrawingCrop]:
    try:
        rendered = renderer(file_path, regions, max_pages=max_pages)
        return [DrawingCrop(**item) for item in rendered]
    except DrawingPipelineError:
        raise
    except Exception as exc:
        raise DrawingPipelineError(
            "drawing_crop_plan_invalid",
            f"Drawing crop preparation failed: {exc}",
            {"error_type": type(exc).__name__},
        ) from exc
