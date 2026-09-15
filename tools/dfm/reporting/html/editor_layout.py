"""Private offline layout worker. Invoked by editor.py, not an Agent tool."""
from pathlib import Path
import json
import sys


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
            page.set_content(source.read_text(encoding='utf-8'), wait_until='load')
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
