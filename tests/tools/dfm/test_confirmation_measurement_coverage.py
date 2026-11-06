from dataclasses import replace
from unittest.mock import Mock

import pytest

from tools.dfm.contracts import (
    EffectiveRule,
    GeometricResultRecord,
    GeometricRuleBinding,
    GeometricRuleOperand,
    PlanRecord,
)
from tools.dfm.errors import DFMError, ReviewSaveError
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.project.manifest import ManifestStore
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.service import DFMService


@pytest.mark.parametrize("coverage", [None, "complete", "partial", "failed", "unknown"])
@pytest.mark.parametrize("value, outcome", [(2.0, "pass"), (4.0, "fail")])
def test_numeric_measurement_is_evaluated_independently_of_coverage(coverage, value, outcome):
    row = GeometricResultRecord(
        "r",
        "s",
        "wall",
        "G_WALL_THK_MAX",
        "ok",
        value,
        "mm",
        {},
        {"geometry_refs": [], "field_refs": []},
        {} if coverage is None else {"measurement_coverage": {"status": coverage}},
        None,
        {"plan_fragment_ref": "f"},
        "a" * 64,
    )
    binding = GeometricRuleBinding(
        "rule",
        "G_WALL_THK_MAX",
        "R_TEST",
        "<=",
        "maximum",
        feature_refs=["wall"],
    )
    operand = GeometricRuleOperand("actual", "G_WALL_THK_MAX", "maximum", ["wall"])
    plan = PlanRecord(
        "plan",
        "step",
        ["occt_cpp"],
        "ready",
        "now",
        rules={"R_TEST": EffectiveRule(3.0, "mm", "test", "v1", name="Max wall")},
        rule_bindings=[binding],
    )
    engine = EvaluationEngine()
    complete = replace(
        row,
        result_id="complete",
        quality={"measurement_coverage": {"status": "complete"}},
    )
    assert engine._resolve_operand([complete, row], plan, binding, operand).value == value
    evaluations, provenance = engine.evaluate([row], plan)
    assert evaluations[0].outcome == outcome
    assert evaluations[0].actual == value
    assert not provenance[evaluations[0].evaluation_id]["unavailable_operands"]
    # Failed computation, absent values and invalid numbers remain unavailable.
    for invalid in [replace(row, status="failed", value=None),
                    replace(row, value=None), replace(row, value=float("nan")),
                    replace(row, value=float("inf")), replace(row, unit=None)]:
        invalid_evaluations, _ = engine.evaluate([invalid], plan)
        assert invalid_evaluations[0].outcome == "indeterminate"


def test_partial_coverage_extrema_use_normal_rule_comparison():
    partial_max = GeometricResultRecord(
        "r_max",
        "s",
        "wall",
        "G_WALL_THK_MAX",
        "ok",
        4.2,
        "mm",
        {},
        {"geometry_refs": [{"kind": "face", "index": 1}], "field_refs": []},
        {"measurement_coverage": {"status": "partial", "valid_area_ratio": 0.75}},
        None,
        {"plan_fragment_ref": "f"},
        "a" * 64,
    )
    max_binding = GeometricRuleBinding(
        "rule_max",
        "G_WALL_THK_MAX",
        "R_MAX",
        "<=",
        "maximum",
        feature_refs=["wall"],
    )
    max_plan = PlanRecord(
        "plan_max",
        "step",
        ["occt_cpp"],
        "ready",
        "now",
        rules={"R_MAX": EffectiveRule(3.5, "mm", "test", "v1", name="Max wall")},
        rule_bindings=[max_binding],
    )
    engine = EvaluationEngine()
    evals, prov = engine.evaluate([partial_max], max_plan)
    assert evals[0].outcome == "fail"
    assert evals[0].actual == 4.2
    assert not prov[evals[0].evaluation_id]["unavailable_operands"]

    partial_min = GeometricResultRecord(
        "r_min",
        "s",
        "wall",
        "G_WALL_THK_MIN",
        "ok",
        0.45,
        "mm",
        {},
        {"geometry_refs": [{"kind": "face", "index": 2}], "field_refs": []},
        {"measurement_coverage": {"status": "partial", "valid_area_ratio": 0.65}},
        None,
        {"plan_fragment_ref": "f"},
        "a" * 64,
    )
    min_binding = GeometricRuleBinding(
        "rule_min",
        "G_WALL_THK_MIN",
        "R_MIN",
        ">=",
        "minimum",
        feature_refs=["wall"],
    )
    min_plan = PlanRecord(
        "plan_min",
        "step",
        ["occt_cpp"],
        "ready",
        "now",
        rules={"R_MIN": EffectiveRule(0.8, "mm", "test", "v1", name="Min wall")},
        rule_bindings=[min_binding],
    )
    min_evals, min_prov = engine.evaluate([partial_min], min_plan)
    assert min_evals[0].outcome == "fail"
    assert min_evals[0].actual == 0.45
    assert not min_prov[min_evals[0].evaluation_id]["unavailable_operands"]


def test_failed_save_blocks_other_groups_plan_and_start_until_same_edit_succeeds(tmp_path):
    workspace = DFMWorkspace(tmp_path / "dfm")
    manifest = workspace.create_project("review barrier")
    store = ManifestStore(workspace.project_dir(manifest.project_id))
    service = object.__new__(DFMService)
    service._store = lambda project_id: store
    service._analysis = Mock(
        side_effect=ReviewSaveError.from_error(
            DFMError("geometry_invalid", "invalid main wall"), "accepted-edit"
        )
    )
    args = {"project_id": manifest.project_id, "feature_id": "wall"}
    with pytest.raises(DFMError):
        service.analysis("update_feature_geometry", **args)
    calls = service._analysis.call_count
    for action, extra in [
        ("plan", {}),
        ("start", {}),
        ("render_html", {}),
        ("confirm_discovery", {"review_group_ids": ["boss"]}),
        ("remove_review_group", {"review_group_id": "boss"}),
    ]:
        with pytest.raises(DFMError, match="Review saving failed"):
            service.analysis(action, **{**args, **extra})
    assert service._analysis.call_count == calls
    assert ManifestStore(store.project_dir).load().capabilities["review_save_failure"]
    service._analysis.side_effect = None
    service._analysis.return_value = {"ok": True}
    service.analysis("update_feature_geometry", **{**args, "feature_id": "boss"})
    assert store.load().capabilities["review_save_failure"]
    service.analysis("update_feature_geometry", **args)
    assert "review_save_failure" not in store.load().capabilities
    assert service.analysis("plan", project_id=manifest.project_id)["ok"]
