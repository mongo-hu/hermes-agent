"""HTML report integration for completed DFM runs."""

from .generator import render_html_report
from .runtime_adapter import materialize_html_runtime

__all__ = ["materialize_html_runtime", "render_html_report"]
