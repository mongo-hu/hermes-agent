"""Deterministic rendering and semantic cropping for DFM drawings.

This module contains no OCR and no model client. It renders source pages for
the current Hermes conversation model, validates the crop plan proposed by
that model, and renders high-resolution crops in memory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import fitz

    HAS_PYMUPDF = True
except ImportError:
    fitz = None  # type: ignore[assignment]
    HAS_PYMUPDF = False


ALLOWED_REGION_TYPES = frozenset({
    "notes",
    "title_block",
    "materials_bom",
    "assembly_dimensions",
    "manufacturing_callouts",
    "other",
})
ALLOWED_REGION_DECISIONS = frozenset({"keep", "keep_uncertain"})


def dependency_report(suffixes: set[str] | None = None) -> dict[str, Any]:
    del suffixes
    missing = [] if HAS_PYMUPDF else ["pymupdf"]
    return {
        "available": not missing,
        "missing": missing,
        "pymupdf": HAS_PYMUPDF,
        "ocr": False,
    }


def _open_document(path: Path, max_pages: int):
    if not HAS_PYMUPDF:
        raise RuntimeError("pymupdf is required for drawing analysis")
    assert fitz is not None
    document = fitz.open(path)
    if len(document) > max_pages:
        document.close()
        raise ValueError(
            f"Drawing has {len(document)} pages; configured maximum is {max_pages}"
        )
    if len(document) == 0:
        document.close()
        raise ValueError("Drawing contains no renderable pages")
    return document


def process_file(
    file_path: str,
    *,
    max_pages: int = 50,
) -> dict[str, Any]:
    path = Path(file_path)
    with _open_document(path, max_pages) as document:
        pages = [
            {
                "page": page_number,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
            }
            for page_number, page in enumerate(document, start=1)
        ]
    return {
        "pages": pages,
        "diagnostics": {
            "page_count": len(pages),
            "semantic_interpretation": "hermes_agent_event_loop",
            "extraction_mode": "model_planned_semantic_crops",
            "ocr_used": False,
        },
    }


def render_overviews(
    file_path: str,
    *,
    max_pages: int = 50,
    dpi: int = 72,
) -> list[dict[str, Any]]:
    path = Path(file_path)
    rendered: list[dict[str, Any]] = []
    with _open_document(path, max_pages) as document:
        for page_number, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            rendered.append({
                "page": page_number,
                "pixel_width": pixmap.width,
                "pixel_height": pixmap.height,
                "png_bytes": pixmap.tobytes("png"),
            })
    return rendered


def _sanitize_region_id(value: object, page: int, index: int) -> str:
    fallback = f"p{page}_r{index:02d}"
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or fallback)).strip("_")
    return sanitized[:80] or fallback


def _validate_regions(
    raw_regions: object,
    *,
    page_count: int,
    padding: int = 24,
    max_regions: int = 48,
) -> list[dict[str, Any]]:
    if (
        not isinstance(raw_regions, list)
        or not raw_regions
        or len(raw_regions) > max_regions
    ):
        raise ValueError(
            f"Crop plan must contain between 1 and {max_regions} regions"
        )
    regions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_regions, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid crop region #{index}: expected an object")
        decision = str(raw.get("decision") or "")
        if decision not in ALLOWED_REGION_DECISIONS:
            raise ValueError(f"Invalid crop decision for region #{index}: {decision}")
        try:
            page = int(raw["page"])
            values = [int(round(float(value))) for value in raw["bbox_1000"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid crop region #{index}: {raw!r}") from exc
        if page < 1 or page > page_count or len(values) != 4:
            raise ValueError(f"Out-of-range crop region #{index}: {raw!r}")
        left, top, right, bottom = values
        if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
            raise ValueError(f"Invalid normalized bbox for region #{index}: {values}")
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(1000, right + padding)
        bottom = min(1000, bottom + padding)
        if right - left < 35 or bottom - top < 35:
            raise ValueError(f"Crop region #{index} is too small")
        region_type = str(raw.get("type") or "other")
        if region_type not in ALLOWED_REGION_TYPES:
            region_type = "other"
        region_id = _sanitize_region_id(raw.get("region_id"), page, index)
        if region_id in seen_ids:
            region_id = f"{region_id}_{index:02d}"
        seen_ids.add(region_id)
        regions.append({
            "page": page,
            "region_id": region_id,
            "type": region_type,
            "bbox_1000": [left, top, right, bottom],
            "decision": decision,
            "reason": str(raw.get("reason") or "")[:1000],
        })
    return regions


def _horizontal_overlap(left: Any, right: Any) -> float:
    overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    return overlap / max(1.0, min(left.width, right.width))


def _snap_to_text(document: Any, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert fitz is not None
    snapped: list[dict[str, Any]] = []
    for region in regions:
        page = document[region["page"] - 1]
        page_rect = page.rect
        left, top, right, bottom = region["bbox_1000"]
        semantic_rect = fitz.Rect(
            page_rect.width * left / 1000,
            page_rect.height * top / 1000,
            page_rect.width * right / 1000,
            page_rect.height * bottom / 1000,
        )
        text_rects = [
            fitz.Rect(block[:4])
            for block in page.get_text("blocks")
            if str(block[4]).strip()
        ]
        changed = True
        while changed:
            changed = False
            for text_rect in text_rects:
                intersects = not (semantic_rect & text_rect).is_empty
                vertical_gap = max(
                    0.0,
                    text_rect.y0 - semantic_rect.y1,
                    semantic_rect.y0 - text_rect.y1,
                )
                if (
                    (intersects or vertical_gap <= 28.0)
                    and _horizontal_overlap(semantic_rect, text_rect) >= 0.35
                ):
                    expanded = semantic_rect | text_rect
                    if expanded != semantic_rect:
                        semantic_rect = expanded
                        changed = True
        margin_x = page_rect.width * 0.008
        margin_y = page_rect.height * 0.012
        semantic_rect = fitz.Rect(
            semantic_rect.x0 - margin_x,
            semantic_rect.y0 - margin_y,
            semantic_rect.x1 + margin_x,
            semantic_rect.y1 + margin_y,
        ) & page_rect
        snapped.append({
            **region,
            "bbox_1000": [
                max(0, round(semantic_rect.x0 / page_rect.width * 1000)),
                max(0, round(semantic_rect.y0 / page_rect.height * 1000)),
                min(1000, round(semantic_rect.x1 / page_rect.width * 1000)),
                min(1000, round(semantic_rect.y1 / page_rect.height * 1000)),
            ],
        })
    return snapped


def _deduplicate_document_regions(
    regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    first_title_page = min(
        (item["page"] for item in regions if item["type"] == "title_block"),
        default=None,
    )
    return [
        item
        for item in regions
        if item["type"] != "title_block" or item["page"] == first_title_page
    ]


def render_crops(
    file_path: str,
    raw_regions: object,
    *,
    max_pages: int = 50,
    dpi: int = 200,
) -> list[dict[str, Any]]:
    path = Path(file_path)
    with _open_document(path, max_pages) as document:
        regions = _validate_regions(raw_regions, page_count=len(document))
        regions = _deduplicate_document_regions(regions)
        regions = _snap_to_text(document, regions)
        rendered: list[dict[str, Any]] = []
        for region in regions:
            page = document[region["page"] - 1]
            page_rect = page.rect
            left, top, right, bottom = region["bbox_1000"]
            clip = fitz.Rect(
                page_rect.x0 + page_rect.width * left / 1000,
                page_rect.y0 + page_rect.height * top / 1000,
                page_rect.x0 + page_rect.width * right / 1000,
                page_rect.y0 + page_rect.height * bottom / 1000,
            ) & page_rect
            pixmap = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
            rendered.append({
                **region,
                "pixel_width": pixmap.width,
                "pixel_height": pixmap.height,
                "png_bytes": pixmap.tobytes("png"),
            })
    return rendered
