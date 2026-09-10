import json

from tools.dfm.contracts import ArtifactRecord
from tools.dfm.findings import materialize_evaluated_findings


def test_evaluations_and_evidence_normalize_to_stable_finding(tmp_path):
    (tmp_path / "measurements.json").write_text(
        json.dumps({
            "input_sha256": "a" * 64,
            "measurements": [{
                "measurement_id": "measurement-draft",
                "region_refs": ["region.fixed-half"],
            }],
        }),
        encoding="utf-8",
    )
    (tmp_path / "evaluations.json").write_text(
        json.dumps({
            "evaluations": [{
                "evaluation_id": "evaluation-draft",
                "metric_id": "injection.geometry.draft",
                "measurement_ids": ["measurement-draft"],
                "rule_id": "min_draft_deg",
                "rule_version": "1.0.0",
                "rule_hash": "c" * 64,
                "outcome": "fail",
            }]
        }),
        encoding="utf-8",
    )
    (tmp_path / "evidence_records.json").write_text(
        json.dumps({
            "records": [{
                "evidence_id": "evidence-draft",
                "evaluation_ids": ["evaluation-draft"],
            }]
        }),
        encoding="utf-8",
    )
    artifacts = [
        ArtifactRecord("m", "measurements", "measurements.json", "application/json", "now"),
        ArtifactRecord("e", "evaluations", "evaluations.json", "application/json", "now"),
        ArtifactRecord("r", "evidence_records", "evidence_records.json", "application/json", "now"),
    ]

    first = materialize_evaluated_findings(tmp_path, artifacts)
    second = materialize_evaluated_findings(tmp_path, artifacts)

    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
    assert first[0].rule_refs[0] == "min_draft_deg@1.0.0"
    assert first[0].metric_ids == ["injection.geometry.draft"]
    assert first[0].region_refs == ["region.fixed-half"]
    assert first[0].evidence_refs == ["evidence-draft"]
