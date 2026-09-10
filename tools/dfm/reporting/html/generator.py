"""Production wrapper around the validated DFM HTML generator."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from ...errors import DFMError
from .template import DEFAULT_VENDOR_DIR, generate_html


def render_html_report(
    llm_content_path: Path,
    runtime_data_path: Path,
    output_path: Path,
) -> Path:
    """Render one self-contained HTML report from the two stable contracts."""

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    vendor_dir = DEFAULT_VENDOR_DIR.resolve()
    try:
        # The standalone generator prints its output path. Suppress that CLI
        # message here because the DFM service may be hosted over stdio JSON-RPC.
        with redirect_stdout(StringIO()):
            generate_html(
                Path(llm_content_path).resolve(),
                Path(runtime_data_path).resolve(),
                temporary,
                vendor_dir,
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise DFMError(
                "report_generation_failed",
                "The DFM HTML generator did not produce a report.",
            )
        os.replace(temporary, output_path)
    except DFMError:
        raise
    except Exception as exc:
        raise DFMError(
            "report_generation_failed",
            "The DFM HTML report could not be generated.",
            {"error": str(exc)},
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return output_path
