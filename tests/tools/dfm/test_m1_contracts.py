import pytest

from tools.dfm.contracts import (
    EffectiveRule,
    LocalObjectiveWorkerRequest,
    ObjectiveArtifactManifest,
    ObjectiveResultManifest,
    ObjectiveTaskRequest,
    PlanOperation,
    PlanRecord,
    RunRecord,
    RunStatus,
    WorkerEvent,
    ResolvedArgument,
)
from tools.dfm.errors import DFMError


def test_m1_plan_and_run_round_trip_preserves_execution_snapshot():
    plan = PlanRecord(
        "plan_1",
        "step",
        ["step"],
        "ready",
        "2026-07-15T00:00:00Z",
        process="injection",
        process_adapter_version="injection-wall-draft-v1",
        scope_id="injection.wall-draft",
        scope_version="1.0.0",
        input_ids=["input_1"],
        input_hashes={"input_1": "a" * 64},
        rules={
            "min_wall_mm": EffectiveRule(
                value=1.2,
                unit="mm",
                source="injection_scope_default",
            )
        },
        operations=[PlanOperation("geometry.load", "load_geometry", [])],
    )
    run = RunRecord(
        "run_1",
        "step",
        "worker-v1",
        RunStatus.QUEUED,
        "2026-07-15T00:00:01Z",
        "2026-07-15T00:00:01Z",
        plan_id=plan.plan_id,
        plan_snapshot=plan.to_dict(),
    )

    assert PlanRecord.from_dict(plan.to_dict()) == plan
    restored = RunRecord.from_dict(run.to_dict())
    assert restored.plan_id == "plan_1"
    assert restored.plan_snapshot["scope_id"] == "injection.wall-draft"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "type": "mystery"},
        {"schema_version": 1, "type": "progress", "percent": 101},
        {"schema_version": 2, "type": "completed", "path": "result.json"},
    ],
)
def test_worker_event_rejects_invalid_payload(payload):
    with pytest.raises(DFMError) as exc_info:
        WorkerEvent.from_dict(payload)

    assert exc_info.value.code == "worker_event_invalid"


def test_objective_task_and_result_round_trip_exclude_rule_and_render_policy():
    task = ObjectiveTaskRequest(
        schema_version=4,
        run_id="run_1",
        input_sha256="a" * 64,
        input_format="step",
        process="injection",
        scope_id="injection.wall-draft",
        scope_version="2.0.0",
        operations=[
            PlanOperation(
                "geometry.draft",
                "measure_draft",
                arguments={
                    "pull_direction": ResolvedArgument([0, 0, 1], "fact:pull_dir")
                },
            )
        ],
    )
    request = LocalObjectiveWorkerRequest(
        schema_version=1,
        backend_version="worker-v1",
        input_path="inputs/part.step",
        output_dir="runs/run_1/artifacts",
        task=task,
    )
    result = ObjectiveResultManifest(
        schema_version=4,
        producer_version="worker-v1",
        run_id="run_1",
        input_sha256="a" * 64,
        process="injection",
        scope_id="injection.wall-draft",
        scope_version="2.0.0",
        result_path="worker_result.json",
        artifacts=[
            ObjectiveArtifactManifest(
                "measurements",
                "measurements",
                "measurements.json",
                "application/json",
                42,
                "b" * 64,
            )
        ],
    )

    payload = task.to_dict()
    assert "rules" not in payload
    assert "max_evidence_findings" not in payload
    assert "input_path" not in payload
    assert ObjectiveTaskRequest.from_dict(payload) == task
    assert LocalObjectiveWorkerRequest.from_dict(request.to_dict()) == request
    assert ObjectiveResultManifest.from_dict(result.to_dict()) == result


def test_local_objective_worker_request_rejects_removed_schema_2_task():
    task = ObjectiveTaskRequest(
        schema_version=4,
        run_id="run_1",
        input_sha256="a" * 64,
        input_format="step",
        process="injection",
        scope_id="injection.geometry-core",
        scope_version="4.0.0",
        operations=[PlanOperation("geometry.preflight", "geometry_preflight")],
    )
    request = LocalObjectiveWorkerRequest(
        schema_version=1,
        backend_version="occt-dfm-geometry-1.4.1",
        input_path="inputs/part.step",
        output_dir="runs/run_1/artifacts",
        task=task,
    ).to_dict()
    request["task"]["schema_version"] = 2

    with pytest.raises(ValueError, match="Objective task identity is invalid"):
        LocalObjectiveWorkerRequest.from_dict(request)
