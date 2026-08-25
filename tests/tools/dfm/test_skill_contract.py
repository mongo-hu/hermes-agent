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
        "capability",
        "artifact",
    ):
        assert token in text
    assert "Never invent" in text
    assert "confirmed" in text
    assert "geometry_engine_missing" in text
    assert "not_implemented" in text
    assert "confirm_cancel=true" in text
    assert "Never cancel" in text


def test_dfm_skill_defines_the_m1_injection_plan_boundary():
    text = SKILL.read_text(encoding="utf-8")

    assert "injection.default" in text
    assert "action=context" in text
    assert "geometry_engine_missing" in text
    assert "injection" in text
    assert "unsupported_capability" in text
    assert "verification_level=experimental" in text
    assert "never invent" in text.lower()
    assert "standards" in text.lower()
