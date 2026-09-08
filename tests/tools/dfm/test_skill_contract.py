from pathlib import Path


SKILL = Path("skills/manufacturing/dfm-analysis/SKILL.md")


def test_dfm_skill_exists_with_discoverable_metadata():
    text = SKILL.read_text(encoding="utf-8")

    assert text.startswith("---\nname: dfm-analysis\n")
    assert "description: Use when" in text
    assert "STEP" in text and "drawing" in text


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
    assert "every page listed in `available_pages`" in text
    assert "sole drawing-semantic source" in text
    assert "Do not call `drawing_context` or reinterpret OCR" in text
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
    assert "Agent interpretation -> validated persistence -> plan -> start" in text
    assert "never invent" in text.lower()
    assert "standards" in text.lower()
