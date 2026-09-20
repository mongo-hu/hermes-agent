"""Private offline layout worker. Invoked by editor.py, not an Agent tool."""
from pathlib import Path
import json
import re
import sys


_SCRIPT_BLOCK = re.compile(r'<script\b[^>]*>.*?</script\s*>', re.IGNORECASE | re.DOTALL)


def layout_markup(markup: str) -> str:
    """Remove executable payloads that do not contribute to fixed page geometry.

    The production source embeds the complete mesh, scalar fields, PDF and
    Three.js runtime inside scripts. Executing those resources in Playwright a
    second time made layout extraction scale with STEP complexity even though
    the extractor only reads the already-materialized slide DOM and CSS.
    """

    return _SCRIPT_BLOCK.sub('', markup)


def extract(source: Path, script: Path) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={'width': 1600, 'height': 1000},
                                          reduced_motion='reduce', service_workers='block')
            # All source assets are embedded by the existing validated renderer.
            # Never fetch links from report prose or arbitrary local files.
            context.route('**/*', lambda route: route.abort())
            page = context.new_page()
            page.set_default_timeout(30000)
            page.set_content(
                layout_markup(source.read_text(encoding='utf-8')),
                wait_until='domcontentloaded',
            )
            page.evaluate('document.fonts.ready')
            page.add_style_tag(content='.slide{zoom:1!important}')
            return page.evaluate(script.read_text(encoding='utf-8'))
        finally:
            browser.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    source, script, output = map(Path, sys.argv[1:])
    output.write_text(json.dumps(extract(source, script), ensure_ascii=False, allow_nan=False), encoding='utf-8')
