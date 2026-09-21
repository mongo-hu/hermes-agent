from pathlib import Path

from agent.skill_utils import parse_frontmatter


SKILL = Path("skills/manufacturing/dfm-analysis/SKILL.md")


def test_dfm_skill_exists_with_discoverable_metadata():
    text = SKILL.read_text(encoding="utf-8")

    frontmatter, _ = parse_frontmatter(text)
    description = frontmatter["description"]

    assert text.startswith("---\nname: dfm-analysis\n")
    assert len(description) <= 60
    assert "STEP/STP" in description
    assert "PDF" in description
    assert "PNG/JPG" in description
    assert "DFM" in description
    assert "STEP" in text and "drawing" in text
    assert "every attached STEP/STP" in text
    assert "register both before" in text
    assert "never silently select only the CAD input" in text


def test_dfm_skill_prescribes_safe_complete_tool_workflow():
    text = SKILL.read_text(encoding="utf-8")

    for token in (
        "dfm_project",
        "dfm_analysis",
        "create",
        "add_input",
        "status",
        "plan",
        "start",
        "cancel",
        "result",
        "report_context",
        "capability",
        "artifact",
        "drawing_context",
        "submit_crop_plan",
        "submit_observations",
        "fusion_context",
        "submit_fusion_links",
        "render_html",
        "report.html",
    ):
        assert token in text
    assert "Never invent" in text
    assert "confirmed" in text
    assert "dependency_missing" in text
    assert "not_implemented" in text
    assert "current Hermes conversation model" in text
    assert "Do not call a second model endpoint" in text
    assert "non-blocking 2D sidecar" in text
    assert "must never delay 3D `plan` or `start`" in text
    assert "No separate vision API" in text
    assert "do not open a PDF in a browser" in text
    assert "An empty crop plan is recorded as a 2D failure" in text
    assert "sole drawing-semantic source" in text
    assert "Do not call `drawing_context` or reinterpret the drawing" in text
    assert "every persisted `global_note` value" in text
    assert "final report editor" in text
    assert "translate/localize the report prose into the user's language" in text
    assert "do not claim that an unevaluated check passed" in text
    assert "mechanically copied Runtime text" in text
    assert "this is not success" in text
    assert "Only this successful action moves the run to `succeeded`/100%" in text
    assert "do not use terminal/read-file" in text


def test_dfm_skill_defines_the_m1_injection_plan_boundary():
    text = SKILL.read_text(encoding="utf-8")

    assert "injection.default" in text
    assert "action=context" in text
    assert "injection" in text
    assert "unsupported_capability" in text
    assert "full-page crop planning" in text
    assert "never invent" in text.lower()
    assert "standards" in text.lower()
