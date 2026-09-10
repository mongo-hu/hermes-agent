import hashlib
import io
import json
from pathlib import Path

import pytest

from tools.dfm.analyzers.base import AnalyzerContext, CancellationToken
from tools.dfm.analyzers.parasolid import ParasolidAnalyzer
from tools.dfm.backends.nx.contracts import (
    NXCalculatorCapability,
    NXCapability,
    NXJobStatus,
)
from tools.dfm.backends.nx.client import HttpNXBackendClient
from tools.dfm.contracts import (
    InputRecord,
    ObjectiveArtifactManifest,
    ObjectiveResultManifest,
    ObjectiveTaskRequest,
    PlanOperation,
    PlanRecord,
    ResolvedArgument,
    RuleBinding,
    RegionRecord,
)
from tools.dfm.errors import DFMError

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "dfm" / "nx"
SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "tools" / "dfm" / "schemas"


class FakeNXClient:
    def __init__(self):
        self.cancelled = []
        self.submitted = []
        self.payload = json.dumps({
            "schema_version": 1,
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "process": "die_casting",
            "scope_id": "die_casting.topology-baseline",
            "measurements": [],
            "producer_contract": "measurement_only",
        }).encode()

    def capability(self):
        return NXCapability(
            "available",
            "NX2406",
            "plugin-1.0",
            {"parasolid_xt": "available"},
            {
                "inspect_topology": NXCalculatorCapability(
                    "certified", output_quantities=("valid_brep",)
                )
            },
        )

    def submit(self, request, input_path):
        self.submitted.append((request, input_path))
        return NXJobStatus("nxjob_1", "queued")

    def status(self, job_id):
        return NXJobStatus(job_id, "succeeded", "complete", 100)

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return NXJobStatus(job_id, "cancelled")

    def result(self, job_id):
        return ObjectiveResultManifest(
            schema_version=4,
            producer_version="fake-nx-2",
            run_id="run_1",
            input_sha256="a" * 64,
            process="die_casting",
            scope_id="die_casting.topology-baseline",
            scope_version="1.0.0",
            result_path="remote_result.json",
            artifacts=[
                ObjectiveArtifactManifest(
                "measurements",
                "measurements",
                "measurements.json",
                "application/json",
                len(self.payload),
                hashlib.sha256(self.payload).hexdigest(),
            )
            ],
        )

    def download(self, job_id, artifact, target):
        target.write(self.payload)


class TaskContractNXClient(FakeNXClient):
    def __init__(self):
        super().__init__()
        measurements = json.loads(
            (FIXTURE_ROOT / "task_contract_measurements.json").read_text()
        )
        measurements["scope_id"] = "die_casting.topology-baseline"
        self.payloads = {
            "measurements": json.dumps(measurements).encode(),
            "field_draft_fixed_half": (
                FIXTURE_ROOT / "task_contract_scalar_field.json"
            ).read_bytes(),
            "scene_golden_part": (
                FIXTURE_ROOT / "task_contract_render_scene.json"
            ).read_bytes(),
            "topology_golden_part": (
                FIXTURE_ROOT / "task_contract_topology_map.json"
            ).read_bytes(),
        }
        self.payload = self.payloads["measurements"]

    def capability(self):
        return NXCapability.from_dict(
            json.loads((FIXTURE_ROOT / "task_contract_capability.json").read_text())
        )

    def result(self, job_id):
        definitions = (
            ("measurements", "measurements", "measurements.json"),
            ("field_draft_fixed_half", "scalar_field", "draft_field.json"),
            ("scene_golden_part", "render_scene", "render_scene.json"),
            ("topology_golden_part", "topology_map", "topology_map.json"),
        )
        artifacts = [
            ObjectiveArtifactManifest(
                artifact_id,
                kind,
                filename,
                "application/json",
                len(self.payloads[artifact_id]),
                hashlib.sha256(self.payloads[artifact_id]).hexdigest(),
            )
            for artifact_id, kind, filename in definitions
        ]
        return ObjectiveResultManifest(
            schema_version=4,
            producer_version="fake-nx-2",
            run_id="run_1",
            input_sha256="a" * 64,
            process="die_casting",
            scope_id="die_casting.topology-baseline",
            scope_version="1.0.0",
            result_path="remote_result.json",
            artifacts=artifacts,
        )

    def download(self, job_id, artifact, target):
        target.write(self.payloads[artifact.artifact_id])


def _context(tmp_path: Path):
    input_path = tmp_path / "inputs" / "part.x_t"
    input_path.parent.mkdir(exist_ok=True)
    input_path.write_text("Parasolid transmit text", encoding="ascii")
    input_record = InputRecord(
        "input_parasolid_1",
        "parasolid",
        "part.x_t",
        "inputs/part.x_t",
        input_path.stat().st_size,
        "a" * 64,
        "now",
        format_id="parasolid_xt",
        representation="brep",
    )
    plan = PlanRecord(
        "plan_1",
        "parasolid",
        ["parasolid"],
        "ready",
        "now",
        process="die_casting",
        scope_id="die_casting.topology-baseline",
        scope_version="1.0.0",
        input_ids=[input_record.input_id],
        operations=[
            PlanOperation("geometry.load", "load_geometry"),
            PlanOperation("geometry.topology", "inspect_topology", ["geometry.load"]),
        ],
    )
    return AnalyzerContext(
        "dfm_1", tmp_path, "parasolid", [input_record], "run_1", plan
    )


def test_nx_contract_fixtures_match_formal_json_schemas():
    jsonschema = pytest.importorskip("jsonschema")
    operation = json.loads((FIXTURE_ROOT / "task_contract_request.json").read_text())
    request = {
        "schema_version": 4,
        "input": {
            "input_id": "input_1",
            "sha256": "a" * 64,
            "format_id": "parasolid_xt",
        },
        "task": {
            "schema_version": 4,
            "run_id": "run_1",
            "input_sha256": "a" * 64,
            "input_format": "parasolid_xt",
            "process": "die_casting",
            "scope_id": "die_casting.golden-product",
            "scope_version": "1.0.0",
            "operations": [operation],
            "regions": [json.loads((FIXTURE_ROOT / "task_contract_region.json").read_text())],
        },
    }
    capability = json.loads(
        (FIXTURE_ROOT / "task_contract_capability.json").read_text()
    )
    measurements = json.loads(
        (FIXTURE_ROOT / "task_contract_measurements.json").read_text()
    )
    result_artifacts = []
    for artifact_id, kind, filename, fixture_name in (
        ("measurements", "measurements", "measurements.json", "task_contract_measurements.json"),
        ("field_draft_fixed_half", "scalar_field", "draft_field.json", "task_contract_scalar_field.json"),
        ("scene_golden_part", "render_scene", "render_scene.json", "task_contract_render_scene.json"),
        ("topology_golden_part", "topology_map", "topology_map.json", "task_contract_topology_map.json"),
    ):
        content = (FIXTURE_ROOT / fixture_name).read_bytes()
        result_artifacts.append(
            ObjectiveArtifactManifest(
                artifact_id,
                kind,
                filename,
                "application/json",
                len(content),
                hashlib.sha256(content).hexdigest(),
            ).to_dict()
        )
    result_manifest = {
        "schema_version": 4,
        "producer_version": "nx-golden-fixture-2",
        "run_id": "run_1",
        "input_sha256": "a" * 64,
        "process": "die_casting",
        "scope_id": "die_casting.golden-product",
        "scope_version": "1.0.0",
        "result_path": "remote_result.json",
        "artifacts": result_artifacts,
    }

    for payload, schema_name in (
        (request, "nx_request.schema.json"),
        (request["task"], "objective_task.schema.json"),
        (result_manifest, "objective_result_manifest.schema.json"),
        (capability, "nx_capability.schema.json"),
        (measurements, "measurement.schema.json"),
        (
            json.loads((FIXTURE_ROOT / "task_contract_scalar_field.json").read_text()),
            "scalar_field.schema.json",
        ),
        (
            json.loads((FIXTURE_ROOT / "task_contract_render_scene.json").read_text()),
            "render_scene.schema.json",
        ),
        (
            json.loads((FIXTURE_ROOT / "task_contract_topology_map.json").read_text()),
            "topology_map.schema.json",
        ),
    ):
        schema = json.loads((SCHEMA_ROOT / schema_name).read_text())
        jsonschema.Draft202012Validator(schema).validate(payload)

    region_schema = json.loads((SCHEMA_ROOT / "region.schema.json").read_text())
    region = json.loads((FIXTURE_ROOT / "task_contract_region.json").read_text())
    jsonschema.Draft202012Validator(region_schema).validate(region)
    binding_schema = json.loads(
        (SCHEMA_ROOT / "rule_binding.schema.json").read_text()
    )
    binding = RuleBinding(
        "binding.draft.fixed_half",
        "draft.fixed_half",
        "dc.geometry.draft.fixed_half",
        "draft_angle_deg",
        "die_casting.min_draft.fixed_half",
        ">=",
        "minimum",
    )
    jsonschema.Draft202012Validator(binding_schema).validate(binding.to_dict())


def test_http_client_wraps_common_task_without_mutating_it(tmp_path, monkeypatch):
    input_path = tmp_path / "part.x_t"
    input_path.write_bytes(b"parasolid")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    task = ObjectiveTaskRequest(
        4,
        "run_1",
        digest,
        "parasolid_xt",
        "injection",
        "injection.wall-draft",
        "2.0.0",
        [PlanOperation("geometry.load", "load_geometry")],
        [],
    ).to_dict()
    calls = []
    client = HttpNXBackendClient("https://nx.example")

    def fake_json(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/v1/inputs":
            return {"input_id": "input_1", "upload_required": False}
        return {"job_id": "job_1", "status": "queued"}

    monkeypatch.setattr(client, "_json", fake_json)

    client.submit(task, input_path)

    envelope = calls[-1][2]
    assert envelope["task"] == task
    assert envelope["input"] == {
        "input_id": "input_1",
        "sha256": digest,
        "format_id": "parasolid_xt",
    }
    assert set(envelope) == {"schema_version", "input", "task"}


def test_parasolid_analyzer_uses_http_client_contract_and_downloads_measurements(
    tmp_path,
):
    client = FakeNXClient()
    analyzer = ParasolidAnalyzer(client, poll_interval_seconds=0)

    capability = analyzer.capability(_context(tmp_path))
    artifacts = analyzer.run(_context(tmp_path), CancellationToken())

    assert capability.status.value == "available"
    assert client.submitted[0][0]["process"] == "die_casting"
    assert client.submitted[0][0]["scope_id"] == "die_casting.topology-baseline"
    assert artifacts[0].kind == "measurements"
    assert (tmp_path / artifacts[0].relative_path).is_file()


def test_http_client_is_not_replaced_by_local_execution_when_unconfigured(tmp_path):
    analyzer = ParasolidAnalyzer()

    capability = analyzer.capability(_context(tmp_path))

    assert capability.status.value == "dependency_missing"
    assert capability.details["transport"] == "http"


def test_parasolid_analyzer_uses_production_contract_for_metric_scoped_operations(tmp_path):
    client = TaskContractNXClient()
    context = _context(tmp_path)
    context.plan.operations.append(
        PlanOperation(
            operation_id="draft.fixed_half",
            calculator_id="measure_draft",
            depends_on=["geometry.topology"],
            metric_ids=["dc.geometry.draft.fixed_half"],
            required_quantities=["draft_angle_deg"],
            required_artifacts=_task_artifacts(),
            required_fact_names=["pull_dir"],
            feature_refs=["feature.screw_boss.003"],
            region_refs=["region.fixed_half"],
            arguments=_task_arguments(),
        )
    )
    context.plan.regions.append(
        _task_region()
    )

    analyzer = ParasolidAnalyzer(client, poll_interval_seconds=0)
    artifacts = analyzer.run(context, CancellationToken())

    request = client.submitted[0][0]
    assert request["schema_version"] == 4
    assert request["operations"][-1] == json.loads(
        (FIXTURE_ROOT / "task_contract_request.json").read_text()
    )
    assert {item.kind for item in artifacts} == {
        "measurements",
        "scalar_field",
        "render_scene",
        "topology_map",
        "worker_result",
    }


def test_parasolid_capability_rejects_arguments_outside_certification_scope(
    tmp_path,
):
    client = TaskContractNXClient()
    context = _context(tmp_path)
    context.plan.operations.append(
        PlanOperation(
            operation_id="draft.fixed_half",
            calculator_id="measure_draft",
            depends_on=["geometry.topology"],
            metric_ids=["dc.geometry.draft.fixed_half"],
            required_quantities=["draft_angle_deg"],
            required_artifacts=_task_artifacts(),
            required_fact_names=["pull_dir"],
            feature_refs=["feature.screw_boss.003"],
            region_refs=["region.fixed_half"],
            arguments={},
        )
    )

    capability = ParasolidAnalyzer(client).capability(context)

    assert capability.status.value == "not_implemented"
    assert capability.details["incompatible_operation_contracts"][0]["reasons"] == [
        "required_arguments",
        "region_unresolved",
    ]


def test_parasolid_analyzer_rejects_measurement_linked_to_wrong_metric(tmp_path):
    client = TaskContractNXClient()
    payload = json.loads(client.payload)
    payload["measurements"][0]["metric_id"] = "dc.geometry.draft.moving_half"
    client.payload = json.dumps(payload).encode()
    client.payloads["measurements"] = client.payload
    context = _context(tmp_path)
    context.plan.operations.append(
        PlanOperation(
            operation_id="draft.fixed_half",
            calculator_id="measure_draft",
            depends_on=["geometry.topology"],
            metric_ids=["dc.geometry.draft.fixed_half"],
            required_quantities=["draft_angle_deg"],
            required_artifacts=_task_artifacts(),
            required_fact_names=["pull_dir"],
            feature_refs=["feature.screw_boss.003"],
            region_refs=["region.fixed_half"],
            arguments=_task_arguments(),
        )
    )
    context.plan.regions.append(
        _task_region()
    )

    with pytest.raises(DFMError) as exc_info:
        ParasolidAnalyzer(client, poll_interval_seconds=0).run(
            context, CancellationToken()
        )

    assert exc_info.value.code == "objective_result_invalid"


def _task_arguments():
    operation = json.loads((FIXTURE_ROOT / "task_contract_request.json").read_text())
    return {
        key: ResolvedArgument.from_dict(value)
        for key, value in operation["arguments"].items()
    }


def _task_region():
    return RegionRecord.from_dict(
        json.loads((FIXTURE_ROOT / "task_contract_region.json").read_text())
    )


def _task_artifacts():
    operation = json.loads((FIXTURE_ROOT / "task_contract_request.json").read_text())
    return operation["required_artifacts"]
