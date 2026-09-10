"""Optional real-PythonOCC test probe."""

from __future__ import annotations

import importlib.util


def occ_available() -> bool:
    try:
        return importlib.util.find_spec("OCC") is not None
    except (ImportError, AttributeError, ValueError):
        return False
