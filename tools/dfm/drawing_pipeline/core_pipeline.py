"""Low-level, model-free OCR extraction for DFM drawings.

This module deliberately knows nothing about Hermes manifests or conversations.
It returns source fragments to the adapter in ``interface.py``. Semantic
interpretation belongs to the Hermes Agent event loop so the drawing pipeline
does not create a second model client or credential path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


try:
    from rapidocr_onnxruntime import RapidOCR

    HAS_RAPIDOCR = True
except ImportError:
    RapidOCR = None  # type: ignore[assignment]
    HAS_RAPIDOCR = False

try:
    import fitz

    HAS_PYMUPDF = True
except ImportError:
    fitz = None  # type: ignore[assignment]
    HAS_PYMUPDF = False


_READER = None


def dependency_report(suffixes: set[str] | None = None) -> dict[str, Any]:
    requested = {item.lower() for item in suffixes or set()}
    missing: list[str] = []
    if not HAS_RAPIDOCR:
        missing.append("rapidocr-onnxruntime")
    if ".pdf" in requested and not HAS_PYMUPDF:
        missing.append("pymupdf")
    return {
        "available": not missing,
        "missing": sorted(set(missing)),
        "rapidocr": HAS_RAPIDOCR,
        "pymupdf": HAS_PYMUPDF,
    }


def _ocr_engine():
    global _READER
    if not HAS_RAPIDOCR:
        raise RuntimeError("rapidocr-onnxruntime is required for drawing OCR")
    if _READER is None:
        assert RapidOCR is not None
        _READER = RapidOCR()
    return _READER


def _bbox(points: Any) -> list[float] | None:
    if not isinstance(points, (list, tuple)) or not points:
        return None
    try:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
    except (IndexError, TypeError, ValueError):
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _fragments(result: Any, page: int) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    for line in result or []:
        if not isinstance(line, (list, tuple)) or len(line) < 2:
            continue
        text = str(line[1] or "").strip()
        if not text:
            continue
        confidence = 0.0
        if len(line) > 2:
            try:
                confidence = float(line[2])
            except (TypeError, ValueError):
                confidence = 0.0
        fragments.append({
            "text": text,
            "page": page,
            "bbox": _bbox(line[0]),
            "confidence": max(0.0, min(confidence, 1.0)),
        })
    return fragments


def _image_fragments(path: Path) -> list[dict[str, Any]]:
    result, _elapsed = _ocr_engine()(str(path))
    return _fragments(result, 1)


def _pdf_fragments(path: Path, max_pages: int) -> list[dict[str, Any]]:
    if not HAS_PYMUPDF:
        raise RuntimeError("pymupdf is required for PDF drawing analysis")
    fragments: list[dict[str, Any]] = []
    assert fitz is not None
    with fitz.open(path) as document:
        if len(document) > max_pages:
            raise ValueError(
                f"Drawing has {len(document)} pages; configured maximum is {max_pages}"
            )
        for page_number, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            result, _elapsed = _ocr_engine()(pixmap.tobytes("png"))
            fragments.extend(_fragments(result, page_number))
    return fragments


def process_file(
    file_path: str,
    *,
    max_pages: int = 50,
) -> tuple[dict[str, Any], str]:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        fragments = _image_fragments(path)
    elif suffix == ".pdf":
        fragments = _pdf_fragments(path, max_pages)
    else:
        raise ValueError(f"Unsupported drawing format: {suffix}")

    raw_text = "\n".join(item["text"] for item in fragments)
    return (
        {
            "fragments": fragments,
            "diagnostics": {
                "ocr_fragment_count": len(fragments),
                "semantic_interpretation": "hermes_agent_event_loop",
            },
        },
        raw_text,
    )
