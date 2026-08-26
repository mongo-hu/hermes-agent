import json

from tools.dfm.contracts import ArtifactRecord, PlanRecord
from tools.dfm.reporting import result_assembler


def test_shared_report_assembles_failed_evaluation_and_evidence(tmp_path, monkeypatch):
    run_id = "run_1"
    output = tmp_path / "runs" / run_id / "artifacts"
    output.mkdir(parents=True)
    payloads = {
        "measurements.json": {
            "input_sha256": "a" * 64,
            "measurements": [{
                "measurement_id": "measurement-draft",
                "algorithm_version": "pythonocc-objective-v4",
                "quality": {"backend": "pythonocc_demo", "certified": False},
            }],
        },
        "evaluations.json": {
            "evaluations": [
                {
                    "evaluation_id": "evaluation-draft",
                    "metric_id": "injection.geometry.draft",
                    "measurement_ids": ["measurement-draft"],
                    "rule_id": "min_draft_deg",
                    "rule_version": "1.0.0",
                    "rule_hash": "b" * 64,
                    "operator": ">=",
                    "actual": 0.5,
                    "expected": 1.0,
                    "outcome": "fail",
                },
                {
                    "evaluation_id": "evaluation-wall",
                    "measurement_ids": [],
                    "outcome": "pass",
                },
            ]
        },
        "evidence_records.json": {
            "records": [{
                "evidence_id": "evidence-draft",
                "evaluation_ids": ["evaluation-draft"],
                "artifact_ref": "image-draft",
            }]
        },
    }
    artifacts = []
    for index, (name, payload) in enumerate(payloads.items()):
        path = output / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        kind = name.removesuffix(".json")
        artifacts.append(
            ArtifactRecord(
                f"artifact-{index}",
                kind,
                path.relative_to(tmp_path).as_posix(),
                "application/json",
                "now",
            )
        )
    image = output / "evidence_001.png"
    image.write_bytes(b"image")
    artifacts.append(
        ArtifactRecord(
            "image-draft",
            "evidence_image",
            image.relative_to(tmp_path).as_posix(),
            "image/png",
            "now",
        )
    )
    plan = PlanRecord(
        "plan_1",
        "step",
        ["step"],
        "ready",
        "now",
        process="injection",
        scope_id="injection.wall-draft",
        scope_version="1.0.0",
    )
    monkeypatch.setattr(result_assembler, "pptx_available", lambda: False)

    generated = result_assembler.materialize_result_reports(
        tmp_path, run_id, plan, artifacts
    )

    assert {item.kind for item in generated} == {"report_json", "report_markdown"}
    report = json.loads((output / "dfm_report.json").read_text(encoding="utf-8"))
    assert len(report["issues"]) == 1
    assert report["issues"][0]["images"] == ["evidence_001.png"]
    assert report["issues"][0]["metric"]["backend"] == "pythonocc_demo"
    assert report["issues"][0]["metric"]["certified"] is False
