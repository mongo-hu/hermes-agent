"""Real Desktop acceptance helpers; isolate windows/state, never replace IPC."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
import time
from urllib.request import urlopen

import pytest


@contextmanager
def review_http_server(service, monkeypatch):
    import uvicorn
    import hermes_cli.web_server as web_server
    from hermes_cli.banner import VERSION
    from hermes_cli.config import ensure_hermes_home

    # Prepare only the conftest-created temporary HERMES_HOME before requests.
    ensure_hermes_home()
    # The full Desktop also opens its normal gateway WebSocket. Import the
    # real handler before serving requests so cold imports cannot block the
    # single event loop while DFM requests are already subject to IPC timeouts.
    import tui_gateway.ws  # noqa: F401

    # Keep the unrelated startup update checker offline in this temporary
    # profile; an unknown cached result avoids both git fetch and PyPI calls.
    (Path(os.environ["HERMES_HOME"]) / ".update_check").write_text(
        json.dumps({"ts": time.time(), "behind": None,
                    "rev": os.environ.get("HERMES_REVISION") or None, "ver": VERSION}),
        encoding="utf-8",
    )
    # models.dev has a separate mtime-based one-hour cache from model_catalog.
    # A fresh, valid minimal registry keeps the unrelated unauthenticated model
    # picker offline; these Desktop tests never select or call an LLM.
    (Path(os.environ["HERMES_HOME"]) / "models_dev_cache.json").write_text(
        json.dumps({"openai": {"id": "openai", "name": "OpenAI", "models": {}}}),
        encoding="utf-8",
    )
    # The real main window requests model options while async /api/config
    # acquires the same synchronous profile lock. Prepare that cold path in
    # this credential-free temporary profile before DFM IPC timeout budgets
    # start; keep the actual HTTP handlers and their subsequent calls intact.
    web_server.get_model_options()
    # /api/status also resolves deferred platform adapters synchronously.
    # Load their real config/registrations before the HTTP loop starts so a
    # fresh test process does not spend DFM request budgets on cold imports.
    # Loading config does not instantiate or connect the registered adapters.
    from gateway.config import load_gateway_config

    load_gateway_config()

    monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: service)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                web_server.app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                lifespan="off",
                timeout_graceful_shutdown=1,
                # Match the actual Desktop loopback server: protocol pings
                # must not disconnect local clients during a busy event loop.
                ws_ping_interval=None,
                ws_ping_timeout=None,
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while (
                not server.started and thread.is_alive() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert server.started, "actual Hermes HTTP server did not start"
            yield {
                "HERMES_DESKTOP_REMOTE_URL": f"http://127.0.0.1:{port}",
                "HERMES_DESKTOP_REMOTE_TOKEN": web_server._SESSION_TOKEN,
            }
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "acceptance HTTP server did not stop"


@contextmanager
def electron_runtime(tmp_path, *, backend=None, software_rendering=False):
    from playwright.sync_api import sync_playwright

    repo = Path(__file__).resolve().parents[3]
    desktop = repo / "apps/desktop"
    electron = repo / "node_modules/electron/dist/electron.exe"
    assert electron.is_file(), "install Desktop workspace dependencies"
    assert (desktop / "dist/electron-main.mjs").is_file(), "build Desktop first"
    assert (desktop / "dist/index.html").is_file(), "build Desktop first"
    wrapper = tmp_path / "electron-acceptance.cjs"
    wrapper.write_text(
        "const { app, BrowserWindow } = require('electron');\n"
        "app.setAsDefaultProtocolClient = () => true;\n"
        f"app.setAppPath({json.dumps(str(desktop))});\n"
        "BrowserWindow.prototype.show = function () {};\n"
        "BrowserWindow.prototype.showInactive = function () {};\n"
        "app.on('browser-window-created', (_event, win) => {\n"
        "  win.hide(); win.setSkipTaskbar(true);\n"
        "  win.webContents.setBackgroundThrottling(false);\n"
        "});\n"
        "process.stdin.on('data', () => app.quit());\n"
        f"import({json.dumps((desktop / 'dist/electron-main.mjs').as_uri())})"
        ".catch(error => { console.error(error); app.exit(1); });\n",
        encoding="utf-8",
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HERMES_DESKTOP_") and key != "ELECTRON_RUN_AS_NODE"
    }
    env.update({
        "HERMES_DESKTOP_USER_DATA_DIR": str(tmp_path / "electron-user-data"),
        "HERMES_DESKTOP_BOOT_FAKE": "1",
        "HERMES_DESKTOP_BOOT_FAKE_STEP_MS": "10",
        "HERMES_DESKTOP_DISABLE_GPU": "0",
        **(backend or {}),
        **({"HERMES_DESKTOP_DEV_SERVER": os.environ["DFM_DESKTOP_E2E_RENDERER_URL"]}
           if os.environ.get("DFM_DESKTOP_E2E_RENDERER_URL") else {}),
    })
    log_path = tmp_path / "electron-acceptance.log"
    # Exercise the Desktop's normal GPU selection by default. Software WebGL
    # remains explicit for diagnostics instead of changing every acceptance run.
    renderer_args = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] if software_rendering else []
    process = None
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                [
                    str(electron),
                    f"--remote-debugging-port={port}",
                    *renderer_args,
                    str(wrapper),
                ],
                cwd=desktop,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            endpoint = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                try:
                    with urlopen(f"{endpoint}/json/version", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("Electron did not expose its acceptance CDP endpoint")
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                try:
                    context = browser.contexts[0]
                    main = context.pages[0]
                    main.wait_for_function(
                        "Boolean(window.hermesDesktop?.openDfmViewerWindow)"
                    )
                    yield main, context
                finally:
                    browser.close()
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.stdin.write(b"quit\n")
                    process.stdin.flush()
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


@contextmanager
def viewer_window(main, context, manifest_path):
    with context.expect_page() as opened:
        result = main.evaluate(
            "path => window.hermesDesktop.openDfmViewerWindow(path)", str(manifest_path)
        )
        assert result["ok"], result
    page = opened.value
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        page.get_by_role("heading", name="DFM 三维模型").wait_for()
        page.locator("canvas").wait_for()
        yield page
        assert not page_errors, page_errors
    except BaseException:
        # These artifacts stay beside this test's isolated viewer manifest.
        # pytest.fail raises an OutcomeException outside Exception; preserve
        # the same failure evidence before re-raising that assertion too.
        # Capture both pending UI and visible errors without hiding the failure.
        artifacts = Path(manifest_path).parent
        try:
            page.screenshot(path=str(artifacts / "desktop-failure.png"))
        except Exception:
            pass
        try:
            body = page.locator("body").inner_text(timeout=2000)
            (artifacts / "desktop-failure.txt").write_text(
                body + "\n\nPage errors:\n" + "\n".join(page_errors),
                encoding="utf-8",
            )
        except Exception:
            pass
        raise
    finally:
        if not page.is_closed():
            page.close()
