"""Production wrapper around the validated DFM HTML generator."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from ...errors import DFMError
from .template import DEFAULT_VENDOR_DIR, generate_html
from .editor import wrap_editor_report


def render_html_report(
    llm_content_path: Path,
    runtime_data_path: Path,
    output_path: Path,
) -> Path:
    """Render one self-contained HTML report from the two stable contracts."""

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The service already passes a UUID-suffixed candidate. Do not concatenate
    # that name again: deep Windows project paths can exceed MAX_PATH.
    temporary = output_path.with_name(f".dfm-{uuid4().hex}.tmp")
    edited = temporary.with_suffix('.editor.tmp')
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
        wrap_editor_report(temporary, Path(llm_content_path).resolve(), Path(runtime_data_path).resolve(), edited)
        if not edited.is_file() or edited.stat().st_size == 0:
            raise DFMError('report_generation_failed', 'The DFM editor did not produce a report.')
        with edited.open('rb+') as stream:
            os.fsync(stream.fileno())
        os.replace(edited, output_path)
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
        edited.unlink(missing_ok=True)
    return output_path
