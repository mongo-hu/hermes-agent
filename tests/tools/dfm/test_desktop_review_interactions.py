"""Focused, real Desktop/HTTP/Worker acceptance for DFM review interactions."""

from io import BytesIO
import json
import os
from pathlib import Path
import re

import pytest

from tests.tools.dfm.desktop_acceptance import (
    electron_runtime,
    review_http_server,
    viewer_window,
)
from tools.dfm.config import DFMConfig
from tools.dfm.service import DFMService


pytestmark = pytest.mark.skipif(
    os.environ.get("DFM_DESKTOP_E2E") != "1",
    reason="explicit real Desktop acceptance is not enabled",
)


@pytest.fixture
def review_project(tmp_path, monkeypatch):
    # conftest already isolates HERMES_HOME; also isolate the profile-list root.
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: user_home))
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "model_catalog:\n  enabled: false\n", encoding="utf-8"
    )
    worker = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE", "")
    assert worker and Path(worker).is_file(), "configure the real Worker"
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
        project_id = service.project("create", name="Review interaction acceptance")[
            "project_id"
        ]
        repo = Path(__file__).resolve().parents[3]
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
        assert any(row["kind"] == "drilled_hole" for row in review["features"])
        refs = wall["properties"]["geometry_refs"]
        # These are supplied review candidates on real topology, not claims that
        # the plate has an automatically recognized/certified undercut.
        service.analysis(
            "restructure_forming_scopes",
            project_id=project_id,
            expected_revision=review["revision"],
            expected_snapshot_id=review["snapshot_id"],
            forming_scopes=[{
                "forming_scope_id": "SIDE_REVIEW",
                "kind": "undercut",
                "mode": "local",
                "geometry_refs": refs,
            }],
        )
        review = service.analysis("review_context", project_id=project_id)
        service.analysis(
            "revise_annotations",
            project_id=project_id,
            expected_revision=review["revision"],
            expected_snapshot_id=review["snapshot_id"],
            annotations=[{
                "annotation_id": "APPEARANCE_REVIEW",
                "kind": "appearance",
                "value": "appearance",
                "geometry_refs": refs[:1],
            }],
        )
        review = service.analysis("review_context", project_id=project_id)
        assert not any(row["kind"] in {"undercut_scope", "local_structure", "local_direction"}
                       and row["status"] != "stale" for row in review["review_groups"])
        scope = next(row for row in review["forming_scopes"]
                     if row["forming_scope_id"] == "SIDE_REVIEW")
        assert scope["status"] == "candidate"
        assert scope["mechanism_kind"] == "undetermined" and scope["draw_direction"] is None
        assert any(row["kind"] == "forming_scope" and row["status"] == "pending_review"
                   and row["target_refs"] == ["SIDE_REVIEW"] for row in review["review_groups"])
        project_dir = service.workspace.project_dir(project_id)
        manifest = next(
            project_dir.glob("artifacts/geometry_sessions/*/dfm_discovery_viewer.json")
        )
        yield service, project_id, manifest
    finally:
        service.close()


def _panel(page):
    from playwright.sync_api import expect

    panel = page.get_by_role("region", name="DFM 分组确认")
    expect(panel).to_be_visible()
    expect(panel.get_by_text("正在读取确认状态…")).not_to_be_visible(timeout=20000)
    return panel


def _bucket(panel, identity):
    labels = {"structure": "结构特征", "annotation": "属性标注", "forming": "出模设置"}
    category = panel.locator(":scope > details").filter(
        has=panel.page.locator("summary", has_text=labels[identity.split(":")[0]])
    )
    if category.get_attribute("open") is None:
        category.locator(":scope > summary").click()
    bucket = panel.locator(f'[data-review-bucket="{identity}"]')
    if bucket.get_attribute("open") is None:
        bucket.locator(":scope > summary").click()
    return bucket


def _picture(page):
    from PIL import Image

    # Pointer movements and overlay margins use CSS pixels, including on HiDPI displays.
    return Image.open(BytesIO(page.locator("canvas").screenshot(scale="css"))).convert("RGB")


def _yellow_pixels(page):
    picture = _picture(page)
    return sum(
        count for count, (red, green, blue) in picture.getcolors(picture.width * picture.height)
        if red > 150 and green > 80 and red > blue * 1.5 and green > blue * 1.2
    )


def _click_blank(page):
    # Pick a visibly empty patch, not an assumed location inside the model.
    picture = _picture(page)
    box = page.locator("canvas").bounding_box()
    for x, y in ((18, 18), (picture.width - 18, 18), (18, picture.height // 2)):
        patch = picture.crop((x - 4, y - 4, x + 4, y + 4))
        if all(max(rgb) < 40 for rgb in patch.getdata()):
            page.mouse.click(
                box["x"] + x * box["width"] / picture.width,
                box["y"] + y * box["height"] / picture.height,
            )
            return
    pytest.fail("No visible empty canvas patch available for deselection")


def _silhouette_box(page):
    from PIL import Image

    picture = _picture(page)
    # Exclude the bottom mouse-help overlay, leaving only the rendered model.
    picture = picture.crop((0, 0, picture.width, picture.height - 40))
    # Locator screenshots contain overlapping sibling UI as well as WebGL.
    # Exclude the passive compass when measuring the CAD silhouette.
    compass = page.get_by_role("img", name="三维方位提示").bounding_box()
    canvas = page.locator("canvas").bounding_box()
    if compass:
        sx = picture.width / canvas["width"]
        sy = (picture.height + 40) / canvas["height"]
        left = max(0, int((compass["x"] - canvas["x"]) * sx) - 4)
        top = max(0, int((compass["y"] - canvas["y"]) * sy) - 4)
        right = min(picture.width, int((compass["x"] + compass["width"] - canvas["x"]) * sx) + 4)
        bottom = min(picture.height, int((compass["y"] + compass["height"] - canvas["y"]) * sy) + 4)
        if left < right and top < bottom:
            picture.paste((0, 0, 0), (left, top, right, bottom))
    mask = Image.new("L", picture.size)
    mask.putdata([255 if max(rgb) > 70 else 0 for rgb in picture.getdata()])
    bounds = mask.getbbox()
    assert bounds is not None, "real geometry must be rendered"
    return bounds


def _model_point(page):
    picture = _picture(page)
    bounds = _silhouette_box(page)
    for y in range((bounds[1] + bounds[3]) // 2, bounds[3] - 5):
        for x in range((bounds[0] + bounds[2]) // 2, bounds[2] - 5):
            patch = picture.crop((x - 3, y - 3, x + 3, y + 3))
            if all(max(rgb) > 70 for rgb in patch.getdata()):
                box = page.locator("canvas").bounding_box()
                return (box["x"] + x * box["width"] / picture.width,
                        box["y"] + y * box["height"] / picture.height)
    pytest.fail("No visible interior face found for a real canvas click")


def _drag_canvas(page, button, dx=55, dy=-30):
    box = page.locator("canvas").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down(button=button)
    page.mouse.move(x + dx, y + dy, steps=12)
    page.mouse.up(button=button)
    page.wait_for_timeout(1100)  # Allow the real renderer to publish the final input frame.


def test_review_selection_pan_and_one_batch_confirmation(
    review_project, tmp_path, monkeypatch
):
    from playwright.sync_api import expect

    service, project_id, manifest = review_project
    review = service.analysis("review_context", project_id=project_id)
    wall = next(row for row in review["features"] if row["kind"] == "main_wall")
    confirmations = []
    analyze = service.analysis

    def observe(action, **params):
        if action == "confirm_discovery":
            confirmations.append(json.loads(json.dumps(params)))
        return analyze(action, **params)

    monkeypatch.setattr(service, "analysis", observe)
    with review_http_server(service, monkeypatch) as backend, electron_runtime(
        tmp_path, backend=backend
    ) as (main, browser), viewer_window(main, browser, manifest) as page:
        panel = _panel(page)
        wall_bucket = _bucket(panel, "structure:main_wall")
        page.wait_for_timeout(550)
        assert _yellow_pixels(page) > 50
        wall_bucket.locator(":scope > summary").click()
        expect(wall_bucket).not_to_have_attribute("open", "")
        assert _yellow_pixels(page) < 10
        wall_bucket = _bucket(panel, "structure:main_wall")
        hole_bucket = _bucket(panel, "structure:drilled_hole")
        expect(wall_bucket).not_to_have_attribute("open", "")
        expect(hole_bucket).to_have_attribute("open", "")

        wall_bucket = _bucket(panel, "structure:main_wall")
        expect(hole_bucket).not_to_have_attribute("open", "")
        row = wall_bucket.locator(f'[data-review-object="{wall["feature_id"]}"]')
        row.get_by_role("checkbox").check()
        title = row.locator(f'button[title="{wall["feature_id"]}"]')
        title.click()
        page.wait_for_timeout(550)
        assert _yellow_pixels(page) > 50
        title.click()
        assert _yellow_pixels(page) < 10
        wall_bucket = _bucket(panel, "structure:main_wall")
        expect(wall_bucket.get_by_role("checkbox")).to_be_checked()
        _click_blank(page)
        assert _yellow_pixels(page) < 10
        wall_bucket = _bucket(panel, "structure:main_wall")
        expect(wall_bucket.get_by_role("checkbox")).to_be_checked()

        # Blank clicks must not clear an in-progress face-selection draft.
        wall_bucket.get_by_role("button", name="编辑选面", exact=True).click()
        editor = page.get_by_role("region", name="编辑特征选面")
        expect(editor).to_be_visible()
        selected_faces = editor.get_by_text(re.compile(r"^已选 \d+ 个面"))
        before_draft = selected_faces.inner_text()
        page.get_by_role("button", name="适合窗口", exact=True).click()
        page.wait_for_timeout(550)
        _click_blank(page)
        expect(selected_faces).to_have_text(before_draft)
        editor.get_by_role("button", name="取消编辑", exact=True).click()
        _click_blank(page)

        page.get_by_role("button", name="适合窗口", exact=True).click()
        page.wait_for_timeout(550)
        point = _model_point(page)
        page.mouse.click(*point)
        expect(page.get_by_text(re.compile(r"^当前面 #\d+$"))).to_be_visible()
        page.mouse.click(*point)
        expect(page.get_by_text(re.compile(r"^当前面 #\d+$"))).to_have_count(0)
        assert _yellow_pixels(page) < 10
        # Leave enough visible margin for both native drags; orthographic Fit
        # intentionally fills more of the canvas than the old sphere-based fit.
        canvas = page.locator("canvas").bounding_box()
        page.mouse.move(canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] / 2)
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(550)
        page.screenshot(path=tmp_path / "review-before-pan.png")
        before = _silhouette_box(page)
        # Orthographic pan translates the silhouette without changing its size.
        _drag_canvas(page, "middle", dx=55, dy=0)
        horizontal = _silhouette_box(page)
        assert horizontal[0] - before[0] > 20 and horizontal[2] - before[2] > 20, (before, horizontal)
        assert abs(horizontal[1] - before[1]) < 5 and abs(horizontal[3] - before[3]) < 5, (before, horizontal)
        page.screenshot(path=tmp_path / "review-horizontal-pan.png")
        _drag_canvas(page, "middle", dx=0, dy=-30)
        page.screenshot(path=tmp_path / "review-after-pan.png")
        after = _silhouette_box(page)
        assert horizontal[1] - after[1] > 10 and horizontal[3] - after[3] > 10, (horizontal, after)
        assert abs(after[0] - horizontal[0]) < 5 and abs(after[2] - horizontal[2]) < 5, (horizontal, after)
        picture = _picture(page)
        for bounds in (before, horizontal, after):
            assert bounds[0] > 5 and bounds[1] > 5 and bounds[2] < picture.width - 5 and bounds[3] < picture.height - 45, bounds
        _drag_canvas(page, "right")
        unchanged = _silhouette_box(page)
        assert max(abs(a - b) for a, b in zip(after, unchanged)) <= 2, (after, unchanged)

        before_review = service.analysis("review_context", project_id=project_id)
        pending = [row for row in before_review["review_groups"] if row["status"] == "pending_review"]
        expected = {row["review_group_id"] for row in pending if row["kind"] in {
            "ordinary_structure", "surface_attribute"
        }}
        unready_plans = {row["review_group_id"] for row in pending if row["kind"] == "forming_scope"}
        assert unready_plans, "the incomplete local plan must remain available for review"
        # The fixture's main direction was already confirmed through confirm_fact.
        assert not any(row["kind"] == "product_forming" for row in pending)
        panel.get_by_role("button", name=f"接受全部可确认项（{len(expected)}）", exact=True).click()
        expect(panel.get_by_role("status")).to_contain_text("已提交本次确认", timeout=45000)
        expect(panel.get_by_role("alert")).to_have_count(0)
        assert len(confirmations) == 1
        assert set(confirmations[0]["review_group_ids"]) == expected
        after_review = service.analysis("review_context", project_id=project_id)
        remaining = [row for row in after_review["review_groups"] if row["status"] == "pending_review"]
        assert all(row["review_group_id"] not in expected for row in remaining)
        assert {row["review_group_id"] for row in remaining} == unready_plans
        assert {row["kind"] for row in remaining} == {"forming_scope"}

        # The confirmed feature list must use the same title/item toggle rules.
        wall_summary = panel.locator("summary").filter(has_text=re.compile(r"^主体壁 · \d+ 个实例$"))
        hole_summary = panel.locator("summary").filter(has_text=re.compile(r"^钻孔 · \d+ 个实例$"))
        wall_summary.click()
        expect(wall_summary.locator("..")).to_have_attribute("open", "")
        page.screenshot(path=tmp_path / "review-batch-confirmation.png")
        hole_summary.click()
        expect(wall_summary.locator("..")).not_to_have_attribute("open", "")
        hole_summary.click()
        expect(hole_summary.locator("..")).not_to_have_attribute("open", "")
        assert _yellow_pixels(page) < 10
        assert len(confirmations) == 1, "accept-all must not loop over newly generated review items"


def test_pending_deletions_survive_refresh_and_worker_reopen(
    review_project, tmp_path, monkeypatch
):
    from playwright.sync_api import expect

    service, project_id, manifest = review_project
    initial = service.analysis("review_context", project_id=project_id)
    hole = next(row for row in initial["features"] if row["kind"] == "drilled_hole")
    original_ids = {row["feature_id"] for row in initial["features"]}
    removed = [hole["feature_id"], "APPEARANCE_REVIEW", "SIDE_REVIEW"]
    with review_http_server(service, monkeypatch) as backend, electron_runtime(
        tmp_path, backend=backend
    ) as (main, browser):
        with viewer_window(main, browser, manifest) as page:
            panel = _panel(page)
            for bucket_id, identity in zip(
                ("structure:drilled_hole", "annotation:appearance", "forming:forming_scope"),
                removed,
            ):
                bucket = _bucket(panel, bucket_id)
                row = bucket.locator(f'[data-review-object="{identity}"]')
                if bucket_id == "forming:forming_scope":
                    row.get_by_role("button", name=re.compile("^" + re.escape(identity) + " ")).click()
                row.get_by_role("button", name="删除", exact=True).click()
                expect(row).to_have_count(0, timeout=30000)
                expect(panel.get_by_role("alert")).to_have_count(0)
                current = service.analysis("review_context", project_id=project_id)
                assert not any(identity in group["target_refs"] for group in current["review_groups"])
                panel.get_by_role("button", name="刷新", exact=True).click()
                expect(panel.locator(f'[data-review-object="{identity}"]')).to_have_count(0)

        service.geometry_sessions.close_project(project_id)
        service.analysis("discover", project_id=project_id)
        restored = service.analysis("review_context", project_id=project_id)
        # Reopen and replay the committed native snapshot, not just its cached UI.
        restored = service.analysis(
            "prepare_review", project_id=project_id,
            expected_revision=restored["revision"],
            expected_snapshot_id=restored["snapshot_id"],
        )
        assert {row["feature_id"] for row in restored["features"]} == original_ids - {hole["feature_id"]}
        assert not any(row["annotation_id"] == "APPEARANCE_REVIEW" and row["status"] != "stale"
                       for row in restored["annotations"])
        assert not any(row["forming_scope_id"] == "SIDE_REVIEW" and row["status"] != "stale"
                       for row in restored["forming_scopes"])
        assert any(row["forming_scope_id"] == "MAIN" for row in restored["forming_scopes"])
        assert not any(set(removed) & set(row["target_refs"]) for row in restored["review_groups"])
        with viewer_window(main, browser, manifest) as page:
            panel = _panel(page)
            for identity in removed:
                expect(panel.locator(f'[data-review-object="{identity}"]')).to_have_count(0)
            assert _silhouette_box(page), "deleting review results must leave the model visible"
            _bucket(panel, "structure:main_wall")
            page.screenshot(path=tmp_path / "review-deletions-restored.png")
