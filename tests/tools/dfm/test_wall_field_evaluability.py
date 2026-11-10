from tools.dfm.contracts import EffectiveRule, GeometricResultRecord, GeometricRuleBinding, PlanRecord
from tools.dfm.evaluation import EvaluationEngine
from tools.dfm.measurement_evaluability import manual_review_results, requires_manual_review
import pytest


@pytest.mark.parametrize("status", ["ok", "unsupported"])
@pytest.mark.parametrize("quality", [
    {"evaluability": {"state": "indeterminate", "reason_codes": ["REASON_INCOMPLETE_COVERAGE"]}},
    {"evaluability": {"reason_codes": ["REASON_UNRESOLVED_VARIABLE_FACE"]}},
    {"thickness_field_coverage": {"coverage_complete": False}},
    {"thickness_field_coverage": {"coverage_complete": True, "measurement_scope": "uniform_wall", "unresolved_variable_face_indices": [3]}},
])
def test_unresolved_wall_evidence_never_enters_rule_operands(status, quality):
    row = GeometricResultRecord("r", "s", "wall", "G_WALL_TRANS_LEN", status,
        10 if status == "ok" else None, "mm" if status == "ok" else None, {},
        {"geometry_refs": [], "field_refs": []}, quality, None, {}, "a" * 64)
    binding = GeometricRuleBinding("rule", "G_WALL_TRANS_LEN", "R_TEST", ">=", "minimum", feature_refs=["wall"])
    plan = PlanRecord("p", "step", ["occt_cpp"], "ready", "now",
        rules={"R_TEST": EffectiveRule(2, "mm", "test", "v1")}, rule_bindings=[binding])
    evaluations, provenance = EvaluationEngine().evaluate([row], plan)
    assert evaluations[0].outcome == "indeterminate"
    assert provenance[evaluations[0].evaluation_id]["unavailable_operands"]
    warnings = manual_review_results([{"result_id": "r", "feature_id": "wall", "quality": quality}])
    assert len(warnings) == 1
    assert warnings[0]["result_id"] == "r"


def test_plain_unsupported_and_legacy_sampling_coverage_do_not_become_field_warnings():
    assert not requires_manual_review({"measurement_coverage": {"status": "partial"}})
    assert manual_review_results([{"status": "unsupported", "quality": {}}]) == []

@pytest.mark.parametrize("review", [False, True])
def test_reports_count_wall_review_separately_from_unsupported_and_failed(tmp_path, review):
    import json
    from tools.dfm.contracts import ArtifactRecord
    from tools.dfm.reporting.result_assembler import materialize_result_reports

    output = tmp_path / "runs" / "wall" / "artifacts"
    output.mkdir(parents=True)
    quality = {"evaluability": {"state": "indeterminate", "reason_codes": ["REASON_INCOMPLETE_COVERAGE"]}} if review else {}
    rows = [
        {"result_id": "wall", "feature_id": "wall", "geometric_id": "G_WALL_TRANS_LEN", "status": "unsupported", "quality": quality},
        {"result_id": "ordinary", "feature_id": "part", "geometric_id": "G_UNSUPPORTED", "status": "unsupported", "quality": {}},
    ]
    artifacts = []
    for kind, payload in [("geometric_results", {"results": rows}), ("evaluations", {"evaluations": []})]:
        path = output / (kind + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        artifacts.append(ArtifactRecord(kind, kind, path.relative_to(tmp_path).as_posix(), "application/json", "now"))
    plan = PlanRecord("plan", "step", ["occt_cpp"], "ready", "now")
    materialize_result_reports(tmp_path, "wall", plan, artifacts)
    report = json.loads((output / "dfm_report.json").read_text(encoding="utf-8"))
    assert report["stats"]["manual_review_measurement_count"] == int(review)
    assert report["stats"]["failed_count"] == 0
    assert len(report["manual_review_results"]) == int(review)
    assert report["analysis_coverage"]["status"] == ("partial" if review else "complete")
