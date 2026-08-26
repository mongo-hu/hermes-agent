from types import SimpleNamespace

from tools.dfm.geometry.step import legacy_analyzer
from tools.dfm.geometry.step.evidence import rendering


def test_evidence_budget_does_not_truncate_findings(monkeypatch, tmp_path):
    issues = [
        legacy_analyzer.Issue(
            f"DFM-{index:03d}",
            "test",
            "Test finding",
            "medium",
            "test",
        )
        for index in range(1, 5)
    ]
    rendered = []
    events = []
    monkeypatch.setattr(
        legacy_analyzer, "shape_bbox", lambda *_args: (0, 0, 0, 1, 1, 1)
    )
    monkeypatch.setattr(
        legacy_analyzer,
        "render_issue_with_vtk",
        lambda _shape, _occ, issue, *_args: (
            rendered.append(issue.id) or [f"{issue.id}.png"]
        ),
    )
    monkeypatch.setattr(
        legacy_analyzer,
        "emit_dfm_event",
        lambda event, **payload: events.append((event, payload)),
    )
    args = SimpleNamespace(
        no_render=False,
        max_evidence_issues=2,
        image_width=1280,
        image_height=860,
        mesh_deflection_mm=0.5,
    )

    rendered_count = rendering.render_issue_evidence(
        object(), object(), issues, tmp_path, args
    )

    assert rendered_count == 2
    assert rendered == ["DFM-001", "DFM-002"]
    assert len(issues) == 4
    assert issues[0].images == ["DFM-001.png"]
    assert issues[2].images == []
    assert events[-1] == (
        "progress",
        {"stage": "evidence_complete", "percent": 78},
    )
