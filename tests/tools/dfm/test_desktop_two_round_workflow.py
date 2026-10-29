"""Native two-round workflow driven through real Desktop confirmation controls."""

from io import BytesIO
import json
import os
from pathlib import Path
import re
import time

import pytest

from tests.tools.dfm.desktop_acceptance import (
    electron_runtime,
    review_http_server,
    viewer_window,
)
from tools.dfm.config import DFMConfig
from tools.dfm.contracts import EffectiveRule, GeometricRuleBinding
from tools.dfm.processes.base import ProcessPlan
from tools.dfm.service import DFMService


pytestmark = pytest.mark.skipif(
    os.environ.get("DFM_DESKTOP_E2E") != "1",
    reason="explicit real Desktop acceptance is not enabled",
)


def wait_run(service, project_id, run_id, stages):
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        run = service.jobs.status(project_id, run_id)
        if run.stage in stages or run.status.value in {
            "failed",
            "blocked",
            "cancelled",
        }:
            return run
        time.sleep(0.02)
    pytest.fail(f"Run remained {run.status.value}/{run.stage}: {run.error}")


def test_desktop_confirms_two_rounds_edits_native_attributes_and_previews_report(
    tmp_path,
    monkeypatch,
):
    from PIL import Image
    from playwright.sync_api import expect

    repo = Path(__file__).resolve().parents[3]
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: user_home))
    worker = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE", "")
    assert worker and Path(worker).is_file(), "configure the real Worker"
    bindings = [
        GeometricRuleBinding(
            "fixture.desktop.wall",
            "G_WALL_THK_LOCAL",
            "fixture.desktop.wall.rule",
            "<=",
            "identity",
            check_id="C_FIXTURE_WALL",
            operand_alias="actual",
        ),
        GeometricRuleBinding(
            "fixture.desktop.draft",
            "G_WALL_OUT_DRAFT",
            "fixture.desktop.draft.rule",
            ">=",
            "identity",
            check_id="C_FIXTURE_DRAFT",
            operand_alias="actual",
        ),
    ]
    rules = ProcessPlan(
        "injection",
        "fixture.desktop.two_rounds",
        "injection.geometry-core",
        "fixture",
        {
            row.rule_id: EffectiveRule(
                0.1 if index == 0 else 1.0,
                "mm" if index == 0 else "degree",
                "fixture:not_production",
            )
            for index, row in enumerate(bindings)
        },
        [],
        {"step"},
        rule_bindings=bindings,
        binding_selectors={
            row.binding_id: {"actual": {"feature_kind": "main_wall"}}
            for row in bindings
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
            geometry_job_poll_interval_ms=20,
        ),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="Desktop two-round acceptance")[
            "project_id"
        ]
        source = repo / "tests/fixtures/dfm/step/injection_plate_with_hole.step"
        uploaded = service.project("add_input", project_id=project_id, path=str(source))
        assert uploaded["preview"]["status"] == "ready"
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
        review = service.analysis("review_context", project_id=project_id)
        wall = next(row for row in review["features"] if row["kind"] == "main_wall")
        original_ids = {row["feature_id"] for row in review["features"]}
        # The scope is an explicitly supplied test candidate, not an assertion
        # that this plate has a certified automatically recognized undercut.
        service.analysis(
            "restructure_forming_scopes",
            project_id=project_id,
            expected_revision=review["revision"],
            forming_scopes=[
                {
                    "forming_scope_id": "SIDE_TEST",
                    "kind": "undercut",
                    "mode": "local",
                    "geometry_refs": wall["properties"]["geometry_refs"],
                }
            ],
        )
        review = service.analysis("review_context", project_id=project_id)
        service.analysis(
            "revise_annotations",
            project_id=project_id,
            expected_revision=review["revision"],
            annotations=[
                {
                    "annotation_id": "APPEARANCE_TEST",
                    "kind": "appearance",
                    "value": "appearance",
                    "geometry_refs": wall["properties"]["geometry_refs"][:1],
                }
            ],
        )
        project_dir = service.workspace.project_dir(project_id)
        discovery = next(
            project_dir.glob("artifacts/geometry_sessions/*/dfm_discovery_viewer.json")
        )
        requests = []
        call = service.geometry_sessions.call

        def observe(pid, method, params):
            if method == "geometrics.compute":
                requests.append(json.loads(json.dumps(params)))
            return call(pid, method, params)

        monkeypatch.setattr(service.geometry_sessions, "call", observe)
        with (
            review_http_server(service, monkeypatch) as backend,
            electron_runtime(tmp_path, backend=backend) as (main, browser),
        ):
            with viewer_window(
                main, browser, Path(uploaded["preview"]["viewer_manifest"])
            ) as preview:
                expect(
                    preview.get_by_text("STEP 预览 · 等待风险分析", exact=False)
                ).to_be_visible()
            with viewer_window(main, browser, discovery) as page:
                panel = page.get_by_role("region", name="DFM 分组确认")
                expect(panel).to_be_visible()
                expect(panel.get_by_text("正在读取确认状态…")).not_to_be_visible(
                    timeout=20000
                )

                def bucket(label):
                    summary = panel.locator("summary").filter(has_text=label)
                    summary.click()
                    return summary.locator("..")

                def confirm_selected(count):
                    button = panel.get_by_role(
                        "button", name=f"确认选中组（{count}）", exact=True
                    )
                    button.click()
                    expect(
                        panel.get_by_role("button", name="确认选中组（0）", exact=True)
                    ).to_be_visible(timeout=20000)
                    expect(panel.get_by_role("alert")).to_have_count(0)

                ordinary = bucket("主体壁")
                ordinary.get_by_role(
                    "checkbox", name=f"选择 {wall['feature_id']}", exact=True
                ).check()
                primary = bucket("第一轮 · 主成型范围")
                primary.get_by_role("checkbox", name="选择 MAIN", exact=True).check()
                confirm_selected(2)
                current = service.analysis("review_context", project_id=project_id)
                assert (
                    next(
                        row
                        for row in current["forming_scopes"]
                        if row["forming_scope_id"] == "MAIN"
                    )["status"]
                    == "confirmed"
                )
                planned = service.analysis("plan", project_id=project_id)
                assert "plan" in planned, planned
                run_id = service.analysis(
                    "start", project_id=project_id, plan_id=planned["plan"]["plan_id"]
                )["run"]["run_id"]
                waiting = wait_run(
                    service, project_id, run_id, {"waiting_review", "waiting_input"}
                )
                assert waiting.stage in {"waiting_review", "waiting_input"}, waiting
                assert waiting.completed_fragment_refs, (
                    "ready main Gs must run before local direction"
                )
                assert not any(row.kind == "report_html" for row in waiting.artifacts)

                # Refresh reads the actual latest revision; no local cache patch.
                panel.get_by_role("button", name="刷新", exact=True).click()
                undercut = bucket("第一轮 · 倒扣范围")
                undercut.get_by_role(
                    "checkbox", name="选择 SIDE_TEST", exact=True
                ).check()
                confirm_selected(1)
                local = bucket("第二轮 · 局部结构")
                local.get_by_role("checkbox", name="选择 SIDE_TEST", exact=True).check()
                confirm_selected(1)
                direction = bucket("第二轮 · 局部退出方向")
                for axis, value in zip("XYZ", ["1", "0", "0"]):
                    direction.get_by_role(
                        "textbox", name=f"SIDE_TEST 方向 {axis}", exact=True
                    ).fill(value)
                direction.get_by_role("button", name="确认此方向", exact=True).click()
                finished = wait_run(
                    service, project_id, run_id, {"report_editing", "complete"}
                )
                assert finished.stage in {"report_editing", "complete"}, finished
                current = service.analysis("review_context", project_id=project_id)
                assert original_ids == {
                    row["feature_id"] for row in current["features"]
                }
                assert any(
                    row["from_ref"] == wall["feature_id"]
                    and row["to_ref"] == "SIDE_TEST"
                    for row in current["relationships"]
                )
                assert next(
                    row
                    for row in current["forming_scopes"]
                    if row["forming_scope_id"] == "SIDE_TEST"
                )["draw_direction"] == [1, 0, 0]
                assert any(
                    item.get("context", {}).get("draw_direction") == [1, 0, 0]
                    for request in requests
                    for item in request["items"]
                )

                # A real user attribute edit must reach Worker without re-running Gs.
                request_count = len(requests)
                panel.get_by_role("button", name="刷新", exact=True).click()
                appearance = bucket("面属性 · 外观")
                appearance.get_by_role("button", name="编辑属性", exact=True).click()
                page.get_by_role("combobox", name="面属性值", exact=True).select_option(
                    "non_appearance"
                )
                page.get_by_role("button", name="保存属性", exact=True).click()
                expect(page.get_by_role("combobox", name="面属性值")).not_to_be_visible(
                    timeout=20000
                )
                current = service.analysis("review_context", project_id=project_id)
                assert (
                    next(
                        row
                        for row in current["annotations"]
                        if row["annotation_id"] == "APPEARANCE_TEST"
                    )["value"]
                    == "non_appearance"
                )
                assert len(requests) == request_count

            artifact = next(
                row for row in finished.artifacts if row.kind == "dfm_viewer"
            )
            with viewer_window(
                main, browser, project_dir / artifact.relative_path
            ) as page:
                canvas = Image.open(
                    BytesIO(page.locator("canvas").screenshot())
                ).convert("RGB")
                assert (
                    sum(
                        count
                        for count, (red, green, blue) in canvas.getcolors(
                            canvas.width * canvas.height
                        )
                        if red > 120 and red > green * 1.5 and red > blue * 1.2
                    )
                    > 50
                )
                assert not any(row.kind == "report_html" for row in finished.artifacts)
                context = service.analysis(
                    "report_context", project_id=project_id, run_id=run_id
                )
                assert context["ready"], context
                accepted = service.analysis(
                    "render_html",
                    project_id=project_id,
                    run_id=run_id,
                    llm_content={
                        "schema_version": "dfm-html-llm/v1",
                        "part": {
                            "name": "Desktop acceptance",
                            "general_tolerance": "Not provided",
                            "technical_note": "Fixture rules, not engineering certification.",
                        },
                        "issues": [
                            {
                                "issue_id": identity,
                                "title": identity,
                                "description": "Deterministic native finding.",
                            }
                            for identity in context["required_issue_ids"]
                        ],
                        "conclusion": {
                            "assessment_level": "以规则结果为准",
                            "summary": "Native workflow acceptance.",
                            "risks": [],
                            "actions": [],
                        },
                    },
                )
                assert accepted["accepted"] is True
                deadline = time.monotonic() + 50
                while time.monotonic() < deadline:
                    final = service.jobs.status(project_id, run_id)
                    if final.status.value != "reporting":
                        break
                    time.sleep(0.02)
                assert final.status.value == "succeeded", final.error
                html = project_dir / next(
                    row.relative_path
                    for row in final.artifacts
                    if row.kind == "report_html"
                )
                target = main.evaluate(
                    "path => window.hermesDesktop.normalizePreviewTarget(path)",
                    str(html),
                )
                assert target["previewKind"] == "html" and target["path"] == str(html)
                page.goto(target["url"])
                expect(page.locator("body")).to_contain_text("Desktop acceptance")
                # The report is the real editor wrapper, not a flat HTML page.
                # Select its summary slide and inspect its live report iframe.
                page.locator('.ed-thumb[data-index="1"]').click()
                page.locator(".ed-present-pill .ed-pill-caret").click()
                page.get_by_role(
                    "button", name=re.compile("在此标签页中演示|Present in this tab")
                ).click()
                report_iframe = page.locator(
                    ".bento-present-overlay section.present iframe.dfm-live-report"
                )
                expect(report_iframe).to_be_visible(timeout=15000)
                if report_iframe.get_attribute("title") != "分析总览":
                    page.keyboard.press("ArrowRight")
                    expect(report_iframe).to_have_attribute("title", "分析总览")
                report_frame = report_iframe.element_handle().content_frame()
                report_frame.get_by_role("button", name="问题定位", exact=True).click()
                report_canvas = report_frame.locator("canvas").first
                expect(report_canvas).to_be_visible(timeout=15000)
                report_image = Image.open(BytesIO(report_canvas.screenshot())).convert(
                    "RGB"
                )
                assert (
                    sum(
                        count
                        for count, (red, green, blue) in report_image.getcolors(
                            report_image.width * report_image.height
                        )
                        if red > 120 and red > green * 1.5 and red > blue * 1.2
                    )
                    > 50
                )
                identities = report_frame.evaluate(
                    "({ mesh: sceneData.render_mesh_snapshot.render_mesh_snapshot_id, topology: topologyData.topology_snapshot.topology_snapshot_id })"
                )
                viewer_data = json.loads(
                    (project_dir / artifact.relative_path).read_text(encoding="utf-8")
                )
                assert identities == {
                    "mesh": viewer_data["render_mesh_snapshot_id"],
                    "topology": viewer_data["topology_snapshot_id"],
                }

        manifest = service._store(project_id).load()
        snapshot = service._latest_discovery_snapshot(manifest)
        state = service.geometry_sessions.call(
            project_id,
            "snapshots.get",
            {
                "snapshot_id": service.geometry_sessions.resolve_snapshot_id(
                    project_id, snapshot.worker_snapshot_id
                ),
            },
        )
        assert state["diagnostics"]["model_import_count"] == 1
        assert (
            next(
                row
                for row in state["annotations"]
                if row["annotation_id"] == "APPEARANCE_TEST"
            )["value"]
            == "non_appearance"
        )
    finally:
        service.close()
