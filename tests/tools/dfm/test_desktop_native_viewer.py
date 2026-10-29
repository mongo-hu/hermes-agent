"""Opt-in real Electron main/preload/renderer with native shared geometry files."""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.request import urlopen

import pytest

from tools.dfm.config import DFMConfig
from tools.dfm.contracts import EffectiveRule, GeometricRuleBinding
from tools.dfm.processes.base import ProcessPlan
from tools.dfm.service import DFMService


pytestmark = pytest.mark.skipif(
    os.environ.get("DFM_DESKTOP_E2E") != "1",
    reason="real Desktop acceptance is explicitly enabled with DFM_DESKTOP_E2E=1",
)


def test_real_desktop_opens_native_preview_discovery_and_early_issue(
    tmp_path, monkeypatch
):
    from PIL import Image
    from playwright.sync_api import sync_playwright

    repo = Path(__file__).resolve().parents[3]
    desktop = repo / "apps/desktop"
    electron = repo / "node_modules/electron/dist/electron.exe"
    worker = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE", "")
    assert worker and Path(worker).is_file(), "configure the actual compiled Worker"
    assert electron.is_file(), "install the Desktop workspace dependencies"
    assert (desktop / "dist/electron-main.mjs").is_file(), "build Desktop first"
    assert (desktop / "dist/index.html").is_file(), "build Desktop first"

    # A non-production rule isolates framework acceptance from remote catalogue data.
    binding = GeometricRuleBinding(
        "fixture.desktop.wall",
        "G_WALL_THK_LOCAL",
        "fixture.desktop.wall.rule",
        "<=",
        "identity",
        check_id="C_FIXTURE_WALL",
        operand_alias="actual",
    )
    rules = ProcessPlan(
        "injection",
        "fixture.desktop",
        "injection.geometry-core",
        "fixture",
        {binding.rule_id: EffectiveRule(0.1, "mm", "fixture:not_production")},
        [],
        {"step"},
        rule_bindings=[binding],
        binding_selectors={
            binding.binding_id: {"actual": {"feature_kind": "main_wall"}}
        },
    )
    monkeypatch.setattr(
        "tools.dfm.service.compile_occt_injection_plan", lambda *_args, **_kwargs: rules
    )

    service = DFMService(
        config=DFMConfig(
            geometry_backend="occt_cpp_external",
            geometry_executable=worker,
            geometry_timeout_seconds=120,
        ),
        reconcile_jobs=False,
    )
    process = None
    try:
        project_id = service.project("create", name="Electron native geometry")[
            "project_id"
        ]
        uploaded = service.project(
            "add_input",
            project_id=project_id,
            path=str(repo / "tests/fixtures/dfm/step/injection_plate_with_hole.step"),
        )
        assert uploaded["preview"]["status"] == "ready"
        preview = Path(uploaded["preview"]["viewer_manifest"])
        for name, value in {
            "process": "injection",
            "model_units": "mm",
            "pull_dir": [0, 0, 1],
            "material": "ABS",
        }.items():
            service.project(
                "confirm_fact", project_id=project_id, fact_name=name, fact_value=value
            )
        service.analysis("discover", project_id=project_id)
        project_dir = service.workspace.project_dir(project_id)
        discovery = next(
            project_dir.glob("artifacts/geometry_sessions/*/dfm_discovery_viewer.json")
        )
        assert json.loads(discovery.read_text(encoding="utf-8"))["features"]
        review = service.analysis("review_context", project_id=project_id)
        service.analysis(
            "confirm_discovery",
            project_id=project_id,
            expected_revision=review["revision"],
            review_group_ids=[
                group["review_group_id"]
                for group in review["review_groups"]
                if group["kind"] in {"ordinary_structure", "product_forming"}
                and group["status"] == "pending_review"
            ],
        )
        planned = service.analysis("plan", project_id=project_id)
        assert "plan" in planned, planned
        run_id = service.analysis(
            "start", project_id=project_id, plan_id=planned["plan"]["plan_id"]
        )["run"]["run_id"]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            run = service.jobs.status(project_id, run_id)
            if (
                run.stage in {"report_editing", "complete"}
                or run.status.value
                in {
                    "failed",
                    "blocked",
                }
                or run.stage in {"waiting_input", "waiting_review"}
            ):
                break
            time.sleep(0.02)
        assert run.stage in {"report_editing", "complete"}, run
        assert not any(row.kind == "report_html" for row in run.artifacts)
        analysis = project_dir / next(
            row.relative_path for row in run.artifacts if row.kind == "dfm_viewer"
        )
        assert json.loads(analysis.read_text(encoding="utf-8"))["issues"]

        # Import the built, actual main process. Only hide windows and isolate state;
        # do not replace IPC handlers, preload, renderer, geometry or file reads.
        wrapper = tmp_path / "electron-acceptance.cjs"
        wrapper.write_text(
            "const { app, BrowserWindow } = require('electron');\n"
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
        })
        log_path = tmp_path / "electron-acceptance.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [
                    str(electron),
                    f"--remote-debugging-port={port}",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                    str(wrapper),
                ],
                cwd=desktop,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
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
                pytest.fail("Electron did not expose its local acceptance CDP endpoint")

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                context = browser.contexts[0]
                main = context.pages[0]
                main.wait_for_function(
                    "Boolean(window.hermesDesktop?.openDfmViewerWindow)"
                )
                invalid = tmp_path / "invalid/dfm_viewer.json"
                invalid.parent.mkdir()
                invalid.write_text(
                    '{"contract_version":"not_a_viewer"}', encoding="utf-8"
                )
                rejected = main.evaluate(
                    "path => window.hermesDesktop.openDfmViewerWindow(path)",
                    str(invalid),
                )
                assert rejected == {"ok": False, "error": "invalid_viewer_manifest"}
                for manifest_path in (preview, discovery, analysis):
                    with context.expect_page() as opened:
                        result = main.evaluate(
                            "path => window.hermesDesktop.openDfmViewerWindow(path)",
                            str(manifest_path),
                        )
                        assert result["ok"], result
                    page = opened.value
                    page.get_by_role("heading", name="DFM 三维模型").wait_for()
                    canvas = page.locator("canvas")
                    canvas.wait_for()
                    canvas_image = Image.open(BytesIO(canvas.screenshot())).convert(
                        "RGB"
                    )
                    assert canvas_image.width > 100 and canvas_image.height > 100
                    assert (
                        len(
                            canvas_image.getcolors(
                                canvas_image.width * canvas_image.height
                            )
                        )
                        > 4
                    )
                    if manifest_path == analysis:
                        colors = canvas_image.getcolors(
                            canvas_image.width * canvas_image.height
                        )
                        red_pixels = sum(
                            count
                            for count, (red, green, blue) in colors
                            if red > 120 and red > green * 1.5 and red > blue * 1.2
                        )
                        assert red_pixels > 50, (
                            "issue overlay must be visible before HTML"
                        )
                    assert not page.get_by_text("DFM 三维结果加载失败").count()
                    payload = page.evaluate(
                        "path => window.hermesDesktop.readJsonFile(path)",
                        str(manifest_path),
                    )
                    assert payload["contract_version"] == "hermes.dfm.viewer/v2"
                    page.close()
                browser.close()
    finally:
        if process is not None and process.poll() is None:
            try:
                process.stdin.write(b"quit\n")
                process.stdin.flush()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=10)
        service.close()
