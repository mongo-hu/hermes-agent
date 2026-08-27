"""Low-level OCR and isolated semantic extraction for DFM drawings.

This module deliberately knows nothing about Hermes manifests or conversations.
It returns extracted document data to the adapter in ``interface.py``; the
adapter is responsible for turning that data into the stable DFM contracts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field


class DfmExtraction(BaseModel):
    material: str | None = Field(
        default=None, description="Material grade, for example ABS"
    )
    general_tolerance: str | None = Field(
        default=None, description="General tolerance standard"
    )
    surface_finish: str | None = Field(
        default=None, description="Surface finish or roughness"
    )
    part_name: str | None = Field(default=None, description="Part name")
    manufacturing_constraints: list[str] = Field(default_factory=list)
    thread_requirements: list[str] = Field(default_factory=list)
    other_global_notes: list[str] = Field(default_factory=list)


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


def run_extraction(
    raw_text: str,
    *,
    model_name: str,
    base_url: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not raw_text.strip():
        return {}, {"status": "skipped_empty_text"}
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key or not model_name.strip():
        return {}, {"status": "disabled_missing_configuration"}

    client_options: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout_seconds,
    }
    if base_url.strip():
        client_options["base_url"] = base_url.strip().rstrip("/")
    client = OpenAI(**client_options)
    try:
        response = client.beta.chat.completions.parse(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract explicitly stated manufacturing facts from a mechanical "
                        "drawing. Do not infer missing values."
                    ),
                },
                {"role": "user", "content": raw_text[:8000]},
            ],
            response_format=DfmExtraction,
            temperature=0.1,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            return {}, {
                "status": "failed",
                "message": "Model returned no structured value",
            }
        return parsed.model_dump(exclude_none=True), {
            "status": "completed",
            "model": model_name,
        }
    except Exception as exc:
        return {}, {
            "status": "failed",
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
            "model": model_name,
        }


def process_file(
    file_path: str,
    *,
    max_pages: int = 50,
    model_name: str = "",
    base_url: str = "",
    timeout_seconds: int = 60,
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
    extraction, semantic = run_extraction(
        raw_text,
        model_name=model_name,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    return (
        {
            "extraction": extraction,
            "fragments": fragments,
            "diagnostics": {
                "ocr_fragment_count": len(fragments),
                "semantic_extraction": semantic,
            },
        },
        raw_text,
    )
