"""Package the validated report in the prebuilt, offline DFM slide editor.

No inputs are rewritten; the immutable source report and its SHA-256 travel
inside the document. All work precedes the generator's atomic output commit.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from ...errors import DFMError
from .template import load_single_jsonl

RUNTIME_DIR = Path(__file__).with_name('editor_runtime')
DOCUMENT_SLOT = '__HERMES_DFM_DOCUMENT_JSON__'


def load_runtime() -> tuple[str, Path]:
    """Reject missing, partial or accidentally mixed runtime releases."""
    try:
        manifest = json.loads((RUNTIME_DIR / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['schema'] != 'dfm-editor-runtime/v1':
            raise ValueError('Unsupported editor runtime manifest')
        assets = {}
        for name in ('shell.html', 'extract.js'):
            # Assets are published with LF. Git's Windows checkout may use
            # CRLF; verify canonical bytes without relaxing content checks.
            raw = (RUNTIME_DIR / name).read_bytes().replace(b'\r\n', b'\n')
            if hashlib.sha256(raw).hexdigest() != manifest['files'][name]:
                raise ValueError(f'Editor runtime checksum mismatch: {name}')
            assets[name] = raw.decode('utf-8')
        if assets['shell.html'].count(DOCUMENT_SLOT) != 1:
            raise ValueError('Editor runtime document slot is missing or ambiguous')
        return assets['shell.html'], RUNTIME_DIR / 'extract.js'
    except (OSError, KeyError, ValueError) as exc:
        raise DFMError('report_generation_failed', 'The packaged DFM editor runtime is missing or invalid.',
                       {'error': str(exc)}) from exc


def validate_layout(layout: dict) -> None:
    """Reject empty/partial layouts before declaring a report deliverable."""
    if not layout.get('slides') or not isinstance(layout.get('records'), dict):
        raise ValueError('No editable report pages were extracted')
    for value in layout['size'].values():
        if not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid report page dimensions')
    seen = set()
    for index, slide in enumerate(layout['slides']):
        if slide['dfmPage'] != index or not slide['elements']:
            raise ValueError('Incomplete report page extraction')
        for element in slide['elements']:
            key = element['id']
            if key in seen or layout['records'][key]['page'] != index:
                raise ValueError('Duplicate or misbound report element')
            seen.add(key)
            for field in ('x', 'y', 'w', 'h'):
                value = element[field]
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f'Invalid element geometry: {key}.{field}')
    if seen != set(layout['records']):
        raise ValueError('Unbound report records')


def wrap_editor_report(source: Path, llm_path: Path, runtime_path: Path, output: Path) -> None:
    shell, extractor = load_runtime()
    raw = source.read_bytes()
    # Run Playwright in its own Python process: the DFM action can be invoked
    # from an asyncio-hosted gateway. No process-global chdir or event-loop edits.
    with TemporaryDirectory(prefix='.dfm-editor-', dir=source.parent) as scratch:
        layout_path = Path(scratch) / 'layout.json'
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('editor_layout.py')),
                 str(source), str(extractor), str(layout_path)],
                cwd=scratch, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise DFMError('report_generation_failed', 'DFM editor layout extraction timed out.') from exc
        if completed.returncode:
            raise DFMError('report_generation_failed',
                           'DFM editor layout extraction failed. Install the DFM Playwright dependency and Chromium browser.',
                           {'error': completed.stderr[-4000:]})
        layout = json.loads(layout_path.read_text(encoding='utf-8'))
    validate_layout(layout)
    llm = load_single_jsonl(llm_path, 'llm_content')
    runtime = load_single_jsonl(runtime_path, 'runtime_data')
    doc = {
        'format': 'bento/slides', 'version': 1, 'docId': str(uuid4()),
        'title': 'DFM 分析报告', 'size': layout['size'],
        'theme': {'background': '#ffffff', 'color': '#1e293b', 'accent': '#087e8b',
                  'fontFamily': '"Microsoft YaHei", sans-serif'},
        'present': {'controls': True, 'progress': True}, 'slides': layout['slides'],
        'dfm': {
            'version': 1, 'sourceSha256': hashlib.sha256(raw).hexdigest(),
            'sourceGzip': base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii'),
            'styles': layout['styles'], 'records': layout['records'],
            'contracts': {
                'llm': llm['schema_version'], 'runtime': runtime['schema_version'],
                # Canonical contract names, not service-internal candidate names.
                'inputHashes': {'llm_content.jsonl': hashlib.sha256(llm_path.read_bytes()).hexdigest(),
                                'runtime_data.jsonl': hashlib.sha256(runtime_path.read_bytes()).hexdigest()},
                'issueIds': sorted(issue['id'] for issue in runtime['report']['issues']),
            },
        },
    }
    payload = json.dumps(doc, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    output.write_text(shell.replace(DOCUMENT_SLOT, payload), encoding='utf-8')
