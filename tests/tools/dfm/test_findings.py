import json

from tools.dfm.contracts import ArtifactRecord
from tools.dfm.findings import materialize_evaluated_findings


def test_evaluations_and_evidence_normalize_to_stable_finding(tmp_path):
    (tmp_path / "measurements.json").write_text(
        json.dumps({
            "input_sha256": "a" * 64,
            "measurements": [
                {
                    "measurement_id": "measurement-draft",
                    "region_refs": ["region.fixed-half"],
                }
            ],
        }),
        encoding="utf-8",
    )
    (tmp_path / "evaluations.json").write_text(
        json.dumps({
            "evaluations": [
                {
                    "evaluation_id": "evaluation-draft",
                    "metric_id": "injection.geometry.draft",
                    "measurement_ids": ["measurement-draft"],
                    "rule_id": "min_draft_deg",
                    "rule_version": "1.0.0",
                    "rule_hash": "c" * 64,
                    "outcome": "fail",
                    "severity": "warning",
                    "severity_rationale": "A reviewed consequence.",
                    "criterion_results": [
                        {"criterion_id": "draft_min", "outcome": "fail"}
                    ],
                }
            ]
        }),
        encoding="utf-8",
    )
    (tmp_path / "evidence_records.json").write_text(
        json.dumps({
            "records": [
                {
                    "evidence_id": "evidence-draft",
                    "evaluation_ids": ["evaluation-draft"],
                }
            ]
        }),
        encoding="utf-8",
    )
    artifacts = [
        ArtifactRecord(
            "m", "measurements", "measurements.json", "application/json", "now"
        ),
        ArtifactRecord(
            "e", "evaluations", "evaluations.json", "application/json", "now"
        ),
        ArtifactRecord(
            "r", "evidence_records", "evidence_records.json", "application/json", "now"
        ),
    ]

    first = materialize_evaluated_findings(tmp_path, artifacts)
    second = materialize_evaluated_findings(tmp_path, artifacts)

    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
    assert first[0].rule_refs[0] == "min_draft_deg@1.0.0"
    assert first[0].metric_ids == ["injection.geometry.draft"]
    assert first[0].region_refs == ["region.fixed-half"]
    assert first[0].evidence_refs == ["evidence-draft"]
    assert first[0].severity == "warning"
    assert first[0].severity_rationale == "A reviewed consequence."
    assert first[0].criterion_results[0]["criterion_id"] == "draft_min"


def test_geometric_results_create_findings_without_legacy_routing(tmp_path):
    (tmp_path / "geometric_results.json").write_text(
        json.dumps({
            "input_sha256": "b" * 64,
            "results": [
                {
                    "result_id": "result-wall",
                    "feature_id": "MAIN_WALL_0001",
                    "geometric_id": "G_WALL_THK_MIN",
                }
            ],
        }),
        encoding="utf-8",
    )
    (tmp_path / "evaluations.json").write_text(
        json.dumps({
            "evaluations": [
                {
                    "evaluation_id": "evaluation-wall",
                    "geometric_id": "G_WALL_THK_MIN",
                    "measurement_ids": ["result-wall"],
                    "rule_id": "R_WALL_MIN",
                    "rule_version": "2",
                    "rule_hash": "c" * 64,
                    "outcome": "fail",
                    "severity": "warning",
                }
            ]
        }),
        encoding="utf-8",
    )
    artifacts = [
        ArtifactRecord(
            "g",
            "geometric_results",
            "geometric_results.json",
            "application/json",
            "now",
        ),
        ArtifactRecord(
            "e", "evaluations", "evaluations.json", "application/json", "now"
        ),
    ]

    finding = materialize_evaluated_findings(tmp_path, artifacts)[0]
    payload = finding.to_dict()

    assert payload["geometric_ids"] == ["G_WALL_THK_MIN"]
    assert payload["feature_refs"] == ["MAIN_WALL_0001"]
    assert "metric_ids" not in payload
    assert "region_refs" not in payload
