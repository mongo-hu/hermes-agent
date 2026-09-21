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
_LIVE_REPORT_SANDBOX = 'o.setAttribute("sandbox","allow-scripts")'
_LIVE_REPORT_SANDBOX_WITH_PDF = (
    'o.setAttribute("sandbox","allow-scripts allow-popups '
    'allow-popups-to-escape-sandbox")'
)
_LIVE_IFRAME_TOKEN = 's=crypto.randomUUID(),l={'
_LIVE_IFRAME_TOKEN_EXPOSED = 's=crypto.randomUUID(),l=(o.dataset.dfmBridge=s,{'
_LIVE_ACTION_PAYLOAD = (
    'textBindings:Object.fromEntries(Object.entries(r.records)'
    '.filter(([,m])=>m.textBindings).map(([m,h])=>[m,h.textBindings]))}'
)
_LIVE_ACTION_PAYLOAD_WITH_ACTIONS = (
    'textBindings:Object.fromEntries(Object.entries(r.records)'
    '.filter(([,m])=>m.textBindings).map(([m,h])=>[m,h.textBindings])),'
    'actions:Object.fromEntries(Object.entries(r.records)'
    '.filter(([,m])=>m.actions?.length).map(([m,h])=>[m,h.actions]))})'
)
_LIVE_ACTION_BINDING = (
    'b.parentElement!==s&&s.append(b),f.add(h.dfmKey);'
    'let x=n.baselines[h.dfmKey]'
)
_LIVE_ACTION_BINDING_WITH_KEYS = (
    'b.parentElement!==s&&s.append(b),f.add(h.dfmKey);'
    'for(let A of n.actions?.[h.dfmKey]??[]){let B=b;'
    'for(let C of A.path)B=B?.childNodes[C];'
    'B?.nodeType===Node.ELEMENT_NODE&&(B.dataset.dfmActionKey=A.key)}'
    'let x=n.baselines[h.dfmKey]'
)
_LIVE_READY_LISTENER = (
    'window.addEventListener("load",()=>{window.dispatchEvent(new Event("resize")),'
    'window.dispatchEvent(new Event("scroll")),u({action:"ready"})})'
)
_LIVE_READY_LISTENER_WITH_ACTIONS = (
    'window.addEventListener("message",h=>{if(h.source===parent&&'
    'h.data?.dfmBridge===n.token&&h.data.action==="activate"&&'
    'typeof h.data.key==="string"){let g=document.querySelector('
    '`[data-dfm-action-key="${CSS.escape(h.data.key)}"]`);g?.click()}}),'
    + _LIVE_READY_LISTENER
)
_SHELL_END = '</script></body></html>'
_EDIT_ACTION_BRIDGE = r'''<script id="dfm-edit-action-bridge">
(()=>{
  let sourceHtml=null;
  let sourcePromise=null;
  const source=()=>{
    if(sourceHtml!==null)return Promise.resolve(sourceHtml);
    const api=window.dfmBento;
    if(!api?.source)return Promise.reject(new Error("DFM source is not ready"));
    sourcePromise??=api.source().then(value=>(sourceHtml=value,value));
    return sourcePromise;
  };
  const warm=()=>{
    if(window.dfmBento?.source)void source().catch(()=>{});
    else requestAnimationFrame(warm);
  };
  const actionAtPoint=(host,x,y)=>[...host.shadowRoot.querySelectorAll(
    "[data-dfm-action-key]"
  )].find(node=>{
    const rect=node.getBoundingClientRect();
    return x>=rect.left&&x<=rect.right&&y>=rect.top&&y<=rect.bottom;
  });
  const openDrawingPdf=()=>{
    const popup=window.open("about:blank","_blank");
    if(!popup){window.dfmBento?.editor?.toast?.("浏览器阻止了二维图纸窗口，请允许弹窗后重试。");return;}
    try{popup.document.body.textContent="正在打开完整二维图纸…";}catch{}
    source().then(html=>{
      const match=html.match(/const embeddedPdfB64 = "([A-Za-z0-9+/=]*)";/);
      if(!match)throw new Error("完整二维图纸数据不存在");
      const raw=atob(match[1]);
      const bytes=Uint8Array.from(raw,char=>char.charCodeAt(0));
      popup.location.replace(URL.createObjectURL(new Blob([bytes],{type:"application/pdf"})));
    }).catch(error=>{
      try{popup.close();}catch{}
      window.dfmBento?.editor?.toast?.(String(error.message||error));
    });
  };
  const activate=async key=>{
    const api=window.dfmBento;
    const page=api?.store?.slide?.dfmPage;
    if(!api?.editor||page===undefined)return;
    api.editor.present(false,false);
    const deadline=performance.now()+5000;
    while(performance.now()<deadline){
      const frame=document.querySelector(
        `.bento-present-overlay iframe.dfm-live-report[data-dfm-page="${page}"][data-ready="true"]`
      );
      const token=frame?.dataset.dfmBridge;
      if(frame?.contentWindow&&token){
        frame.contentWindow.postMessage({dfmBridge:token,action:"activate",key},"*");
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    api.editor.toast?.("交互报告启动超时，请重试。");
  };
  document.addEventListener("click",event=>{
    if(document.querySelector(".bento-present-overlay"))return;
    const host=event.composedPath().find(node=>
      node instanceof HTMLElement&&node.classList?.contains("bento-el")&&node.shadowRoot
    );
    if(!host)return;
    const action=actionAtPoint(host,event.clientX,event.clientY);
    if(!action)return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if(action.dataset.dfmActionKind==="drawing-pdf")openDrawingPdf();
    else void activate(action.dataset.dfmActionKey);
  },true);
  requestAnimationFrame(warm);
})();
</script>'''


def _replace_runtime_marker(shell: str, marker: str, replacement: str, label: str) -> str:
    if shell.count(marker) != 1:
        raise ValueError(f'Editor runtime {label} marker is missing or ambiguous')
    return shell.replace(marker, replacement)


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
        # The integrity-checked original DFM report runs only in the live
        # presentation iframe. Its complete-2D-drawing action opens an embedded
        # PDF Blob in a new tab, which browsers block unless this narrowly
        # scoped sandbox has allow-popups. Keep arbitrary inline handlers
        # stripped from the editable canvas and do not grant same-origin.
        shell = _replace_runtime_marker(
            assets['shell.html'],
            _LIVE_REPORT_SANDBOX,
            _LIVE_REPORT_SANDBOX_WITH_PDF,
            'live-report sandbox',
        )
        shell = _replace_runtime_marker(
            shell, _LIVE_IFRAME_TOKEN, _LIVE_IFRAME_TOKEN_EXPOSED,
            'live-report token',
        )
        shell = _replace_runtime_marker(
            shell, _LIVE_ACTION_PAYLOAD, _LIVE_ACTION_PAYLOAD_WITH_ACTIONS,
            'live-report action payload',
        )
        shell = _replace_runtime_marker(
            shell, _LIVE_ACTION_BINDING, _LIVE_ACTION_BINDING_WITH_KEYS,
            'live-report action binding',
        )
        shell = _replace_runtime_marker(
            shell, _LIVE_READY_LISTENER, _LIVE_READY_LISTENER_WITH_ACTIONS,
            'live-report action listener',
        )
        # The editable clone remains script-free. A click on an extracted DFM
        # action is resolved geometrically, then relayed with a random iframe
        # token to the integrity-checked original report. Nothing auto-starts
        # presentation or requests browser fullscreen.
        shell = _replace_runtime_marker(
            shell, _SHELL_END, f'</script>{_EDIT_ACTION_BRIDGE}</body></html>',
            'document end',
        )
        return shell, RUNTIME_DIR / 'extract.js'
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
            # Level 6 keeps the report self-contained and compact while avoiding
            # level-9 CPU cost on mesh-heavy reports.
            'sourceGzip': base64.b64encode(
                gzip.compress(raw, compresslevel=6, mtime=0)
            ).decode('ascii'),
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
