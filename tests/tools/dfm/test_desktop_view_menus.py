"""Real mouse/renderer/HTTP/Worker acceptance for orthographic view menus."""

import math
import os
import re
import time

import pytest

from tests.tools.dfm.desktop_acceptance import (
    electron_runtime,
    review_http_server,
    viewer_window,
)
from tests.tools.dfm.test_desktop_review_interactions import (
    _bucket,
    _click_blank,
    _drag_canvas,
    _model_point,
    _panel,
    _picture,
    _silhouette_box,
    _yellow_pixels,
    review_project,  # noqa: F401 -- reuse the isolated real Worker fixture
)


pytestmark = pytest.mark.skipif(
    os.environ.get("DFM_DESKTOP_E2E") != "1",
    reason="explicit real Desktop acceptance is not enabled",
)


def _camera(page):
    # Read one rendered state atomically instead of interleaving four IPC reads
    # with camera animation frames and spending the observation budget on IPC.
    state = page.locator('[aria-label="DFM 三维画布"]').evaluate("""el => ({
      projection: el.dataset.cameraProjection,
      direction: JSON.parse(el.dataset.cameraDirection),
      target: JSON.parse(el.dataset.cameraTarget), zoom: Number(el.dataset.cameraZoom)
    })""")
    assert state.pop("projection") == "orthographic"
    return state


def _same_camera(actual, expected):
    assert actual["direction"] == pytest.approx(expected["direction"], abs=1e-6)
    assert actual["target"] == pytest.approx(expected["target"], abs=1e-6)
    assert actual["zoom"] == pytest.approx(expected["zoom"], rel=1e-6)


def _settled_camera(page, *, ready=None):
    # View animations use elapsed time but publish on rendered frames. Wait
    # for the actual state to settle instead of assuming a fixed frame rate.
    # This tolerance is stricter than the later unchanged-center assertions.
    deadline = time.monotonic() + 30
    previous = _camera(page)
    stable_since = None
    stable_samples = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(120)
        current = _camera(page)
        stationary = (
            math.dist(current["target"], previous["target"]) <= 1e-8
            and math.dist(current["direction"], previous["direction"]) <= 1e-8
            and math.isclose(current["zoom"], previous["zoom"], rel_tol=1e-8, abs_tol=1e-10)
        )
        if stationary and (ready is None or ready(current)):
            stable_samples += 1
            if stable_since is None:
                stable_since = time.monotonic()
            if stable_samples >= 5 and time.monotonic() - stable_since >= 0.6:
                return current
        else:
            stable_since = None
            stable_samples = 0
        previous = current
    pytest.fail(f"Real camera did not reach a stable expected state: {previous}")


def _canvas_point(page, predicate):
    # Derive native pointer coordinates from actual rendered pixels, never a
    # presumed model position. Stay clear of the passive bottom-left compass.
    picture = _picture(page)
    box = page.locator("canvas").bounding_box()
    for y in range(20, picture.height - 50, 18):
        for x in range(20, picture.width - 20, 18):
            if x < 120 and y > picture.height - 120:
                continue
            patch = picture.crop((x - 4, y - 4, x + 4, y + 4))
            if all(predicate(rgb) for rgb in patch.getdata()):
                return (
                    box["x"] + x * box["width"] / picture.width,
                    box["y"] + y * box["height"] / picture.height,
                )
    pytest.fail("No suitable visible canvas patch found for a native pointer event")


def _blank_point(page):
    return _canvas_point(page, lambda rgb: max(rgb) < 40)


def _open_view_gesture(page):
    from playwright.sync_api import expect

    x, y = _blank_point(page)
    page.mouse.move(x, y)
    page.mouse.down(button="right")
    page.mouse.move(x + 12, y + 12, steps=3)
    menu = page.get_by_role("group", name="视图方向菜单")
    expect(menu).to_be_visible()
    expect(menu.locator("[data-view-command]")).to_have_count(6)
    box = menu.bounding_box()
    canvas = page.locator("canvas").bounding_box()
    assert box["x"] >= canvas["x"] and box["y"] >= canvas["y"]
    assert box["x"] + box["width"] <= canvas["x"] + canvas["width"] + 1
    assert box["y"] + box["height"] <= canvas["y"] + canvas["height"] + 1
    # An edge menu moves inward. Enter its displayed center to acknowledge
    # that position before sliding toward a command; appearance alone is inert.
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=6)
    return menu, box


def _view_gesture(page, command, screenshot=None):
    from playwright.sync_api import expect

    angles = {"up": 0, "align": 45, "right": 90, "down": 180, "fit": 225, "left": 270}
    directions = {"up": [0, 0, -1], "right": [-1, 0, 0], "down": [0, 0, 1], "left": [1, 0, 0]}
    expected_direction = directions.get(command)
    if command == "align":
        direction = _camera(page)["direction"]
        axis = max(range(3), key=lambda index: abs(direction[index]))
        expected_direction = [0.0, 0.0, 0.0]
        expected_direction[axis] = math.copysign(1, direction[axis])
    menu, box = _open_view_gesture(page)
    angle = math.radians(angles[command])
    radius = box["width"] * 0.34
    page.mouse.move(
        box["x"] + box["width"] / 2 + math.sin(angle) * radius,
        box["y"] + box["height"] / 2 - math.cos(angle) * radius,
        steps=8,
    )
    expect(menu.locator(f'[data-view-command="{command}"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    if screenshot:
        page.screenshot(path=screenshot)
    page.mouse.up(button="right")
    expect(menu).to_have_count(0)
    return _settled_camera(page, ready=lambda state: (
        expected_direction is None or math.dist(state["direction"], expected_direction) <= 1e-6
    ))


def test_orthographic_blank_gestures_pan_zoom_align_and_fit(
    review_project, tmp_path, monkeypatch
):
    from playwright.sync_api import expect

    service, _project_id, manifest = review_project
    with review_http_server(service, monkeypatch) as backend, electron_runtime(
        tmp_path, backend=backend
    ) as (main, browser), viewer_window(main, browser, manifest) as page:
        _panel(page)
        expect(page.get_by_role("img", name="三维方位提示")).to_be_visible()
        initial = _settled_camera(page)
        page.screenshot(path=tmp_path / "orthographic-initial.png")

        # A short blank right-click has no menu and never changes the view.
        page.mouse.click(*_blank_point(page), button="right")
        expect(page.get_by_role("group", name="视图方向菜单")).to_have_count(0)
        expect(page.get_by_role("menu", name="审核对象菜单")).to_have_count(0)
        _same_camera(_camera(page), initial)

        # Lock the context at pointer-down: dragging on actual model geometry
        # must neither rotate the camera nor become a blank-space view gesture.
        x, y = _model_point(page)
        page.mouse.move(x, y)
        page.mouse.down(button="right")
        page.mouse.move(x + 70, y - 40, steps=12)
        expect(page.get_by_role("group", name="视图方向菜单")).to_have_count(0)
        page.mouse.up(button="right")
        expect(page.get_by_role("menu", name="审核对象菜单")).to_have_count(0)
        _same_camera(_settled_camera(page), initial)

        # The exposed direction is camera -> target (the inverse of the named
        # model-side axis). Each command preserves observation center and scale.
        for command, direction in (
            ("right", [-1, 0, 0]), ("left", [1, 0, 0]),
            ("down", [0, 0, 1]), ("up", [0, 0, -1]),
        ):
            before = _camera(page)
            after = _view_gesture(page, command, tmp_path / f"view-menu-{command}.png")
            assert after["direction"] == pytest.approx(direction, abs=1e-6)
            assert after["target"] == pytest.approx(before["target"], abs=1e-6)
            assert after["zoom"] == pytest.approx(before["zoom"], rel=1e-6)

        before_cancel = _camera(page)
        menu, box = _open_view_gesture(page)
        center = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.move(center[0] + box["width"] * 0.34, center[1], steps=6)
        expect(menu.locator('[data-view-command="right"]')).to_have_attribute("aria-pressed", "true")
        page.mouse.move(*center, steps=6)
        expect(menu.locator('[aria-pressed="true"]')).to_have_count(0)
        page.screenshot(path=tmp_path / "view-menu-cancel.png")
        page.mouse.up(button="right")
        expect(menu).to_have_count(0)
        _same_camera(_camera(page), before_cancel)

        menu, _box = _open_view_gesture(page)
        canvas = page.locator("canvas").bounding_box()
        page.mouse.move(canvas["x"] + canvas["width"] + 5, canvas["y"] + 20, steps=8)
        page.mouse.up(button="right")
        expect(menu).to_have_count(0)
        _same_camera(_camera(page), before_cancel)

        # Fit creates a centered reference at this orientation. A true
        # orthographic pan shifts the image without resizing its silhouette.
        centered = _view_gesture(page, "fit", tmp_path / "view-menu-fit.png")
        silhouette = _silhouette_box(page)
        _drag_canvas(page, "middle", dx=40, dy=-20)
        panned = _settled_camera(page, ready=lambda state: math.dist(state["target"], centered["target"]) > 0.01)
        moved = _silhouette_box(page)
        assert panned["direction"] == pytest.approx(centered["direction"], abs=1e-6)
        assert panned["zoom"] == pytest.approx(centered["zoom"], rel=1e-6)
        assert math.dist(panned["target"], centered["target"]) > 0.01
        assert moved[0] - silhouette[0] > 20 and silhouette[1] - moved[1] > 10
        assert abs((moved[2] - moved[0]) - (silhouette[2] - silhouette[0])) <= 3
        assert abs((moved[3] - moved[1]) - (silhouette[3] - silhouette[1])) <= 3
        page.screenshot(path=tmp_path / "orthographic-middle-pan.png")

        page.mouse.move(canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] / 2)
        page.mouse.wheel(0, -240)
        zoomed = _settled_camera(page, ready=lambda state: state["zoom"] > panned["zoom"])
        assert zoomed["zoom"] > panned["zoom"]
        assert zoomed["target"] == pytest.approx(panned["target"], abs=1e-6)
        assert zoomed["direction"] == pytest.approx(panned["direction"], abs=1e-6)
        fitted = _view_gesture(page, "fit")
        assert fitted["target"] == pytest.approx(centered["target"], abs=1e-6)
        assert fitted["direction"] == pytest.approx(zoomed["direction"], abs=1e-6)
        assert fitted["zoom"] == pytest.approx(centered["zoom"], rel=1e-6)
        bounds = _silhouette_box(page)
        picture = _picture(page)
        assert bounds[0] > 5 and bounds[1] > 5
        assert bounds[2] < picture.width - 5 and bounds[3] < picture.height - 45
        page.screenshot(path=tmp_path / "orthographic-fit.png")

        _drag_canvas(page, "left", dx=45, dy=-30)
        oblique = _settled_camera(page, ready=lambda state: math.dist(state["direction"], fitted["direction"]) > 0.01)
        assert max(abs(value) for value in oblique["direction"]) < 0.999
        expected = [0.0, 0.0, 0.0]
        axis = max(range(3), key=lambda index: abs(oblique["direction"][index]))
        expected[axis] = math.copysign(1, oblique["direction"][axis])
        aligned = _view_gesture(page, "align", tmp_path / "view-menu-align.png")
        assert aligned["direction"] == pytest.approx(expected, abs=1e-6)
        assert aligned["target"] == pytest.approx(oblique["target"], abs=1e-6)
        assert aligned["zoom"] == pytest.approx(oblique["zoom"], rel=1e-6)
        page.screenshot(path=tmp_path / "orthographic-aligned.png")


def test_object_menu_select_clear_and_delete_through_real_backend(
    review_project, tmp_path, monkeypatch
):
    from playwright.sync_api import expect

    service, project_id, manifest = review_project
    initial = service.analysis("review_context", project_id=project_id)
    wall = next(row for row in initial["features"] if row["kind"] == "main_wall")
    wall_id = wall["feature_id"]
    other_ids = {row["feature_id"] for row in initial["features"]} - {wall_id}
    with review_http_server(service, monkeypatch) as backend, electron_runtime(
        tmp_path, backend=backend
    ) as (main, browser), viewer_window(main, browser, manifest) as page:
        panel = _panel(page)
        _view_gesture(page, "up")
        _view_gesture(page, "fit")
        _bucket(panel, "structure:main_wall")
        page.wait_for_timeout(350)
        point = _canvas_point(page, lambda rgb: (
            rgb[0] > 150 and rgb[1] > 80 and rgb[0] > rgb[2] * 1.5 and rgb[1] > rgb[2] * 1.2
        ))
        _click_blank(page)
        assert _yellow_pixels(page) < 10
        before = _camera(page)
        shape = _silhouette_box(page)

        # The fixture's local scope shares the wall's faces. A raw face hit is
        # ambiguous, so no destructive command may precede object selection.
        page.mouse.click(*point, button="right")
        menu = page.get_by_role("menu", name="审核对象菜单")
        expect(menu.get_by_text("该面属于多个审核对象")).to_be_visible()
        expect(menu.get_by_role("menuitem", name=re.compile("删除识别项"))).to_have_count(0)
        page.screenshot(path=tmp_path / "object-menu-multiple-owners.png")
        menu.get_by_role("menuitem", name=re.compile(re.escape(wall_id))).click()
        expect(menu.get_by_role("menuitem", name="取消高亮", exact=True)).to_be_visible()
        assert _yellow_pixels(page) > 50
        menu.get_by_role("menuitem", name="取消高亮", exact=True).click()
        expect(menu).to_have_count(0)
        assert _yellow_pixels(page) < 10
        _same_camera(_camera(page), before)

        page.mouse.click(*point, button="right")
        menu.get_by_role("menuitem", name=re.compile(re.escape(wall_id))).click()
        expect(menu.get_by_text("待确认", exact=True)).to_be_visible()
        page.screenshot(path=tmp_path / "object-menu-delete.png")
        menu.get_by_role("menuitem", name=re.compile("^删除识别项")).click()
        expect(panel.locator(f'[data-review-object="{wall_id}"]')).to_have_count(0, timeout=30000)
        expect(panel.get_by_role("alert")).to_have_count(0)
        current = service.analysis("review_context", project_id=project_id)
        assert current["revision"] > initial["revision"]
        assert {row["feature_id"] for row in current["features"]} == other_ids
        assert not any(wall_id in group["target_refs"] for group in current["review_groups"])
        panel.get_by_role("button", name="刷新", exact=True).click()
        expect(panel.locator(f'[data-review-object="{wall_id}"]')).to_have_count(0)
        _same_camera(_camera(page), before)
        assert max(abs(a - b) for a, b in zip(_silhouette_box(page), shape)) <= 2
        page.screenshot(path=tmp_path / "object-menu-deleted-model-preserved.png")


def test_rotation_continues_over_both_poles_after_standard_views(
    review_project, tmp_path, monkeypatch
):
    service, _project_id, manifest = review_project
    with review_http_server(service, monkeypatch) as backend, electron_runtime(
        tmp_path, backend=backend
    ) as (main, browser), viewer_window(main, browser, manifest) as page:
        _panel(page)
        box = page.locator("canvas").bounding_box()
        x = box["x"] + box["width"] * 0.5
        y = box["y"] + box["height"] * 0.45
        # Each equal stroke turns about 60 degrees. Six consecutive strokes
        # must pass both poles and return, rather than freeze at a fixed up axis.
        for view in ("up", "down", "left", "right"):
            start = _view_gesture(page, view)
            before = start
            for stroke in range(6):
                page.mouse.move(x, y)
                page.mouse.down(button="left")
                # Rapid real moves must all count even when rendering is slow;
                # do not insert a frame wait that could hide dropped input.
                for step in range(1, 13):
                    page.mouse.move(x, y + box["height"] * step / 72)
                page.mouse.up(button="left")
                after = _settled_camera(page)
                dot = sum(a * b for a, b in zip(before["direction"], after["direction"]))
                angle = math.acos(max(-1, min(1, dot)))
                assert 0.65 < angle < 1.45, (view, stroke, angle, before, after)
                assert after["target"] == pytest.approx(start["target"], abs=1e-6)
                assert after["zoom"] == pytest.approx(start["zoom"], rel=1e-6)
                before = after
            assert math.dist(after["direction"], start["direction"]) < 0.15
            page.screenshot(path=tmp_path / f"rotation-full-turn-{view}.png")
