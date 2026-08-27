import json
from pathlib import Path
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.occt import discover_geometry_executable
from tools.dfm.config import DFMConfig
from tools.dfm.service import DFMService


EXECUTABLE = discover_geometry_executable()
FIXTURE = Path("tests/fixtures/dfm/step/injection_plate_with_hole.step").resolve()


@pytest.mark.skipif(
    EXECUTABLE is None, reason="real OCCT E2E requires dfm-geometry.exe"
)
def test_real_occt_injection_vertical_slice(tmp_path):
    token = set_hermes_home_override(tmp_path / "home")
    service = DFMService(
        config=DFMConfig(geometry_executable=EXECUTABLE or ""),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="OCCT E2E")["project_id"]
        added = service.project("add_input", project_id=project_id, path=str(FIXTURE))
        assert added["preview"]["status"] == "ready", added["preview"]
        preview_manifest = json.loads(
            Path(added["viewer_manifest"]).read_text(encoding="utf-8")
        )
        assert preview_manifest["status"] == "preview"
        assert preview_manifest["issues"] == []
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="process",
            fact_value="injection",
        )
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="model_units",
            fact_value="mm",
        )
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="material",
            fact_value="ABS",
        )
        service.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="pull_dir",
            fact_value=[0, 0, 1],
        )
        service.analysis("discover", project_id=project_id)
        plan = service.analysis(
            "plan",
            project_id=project_id,
            analyzer_key="occt_cpp",
        )["plan"]
        assert plan["scope_id"] == "injection.default"
        assert plan["ontology_snapshot_id"] == "ontology.injection.default@1.2.0"
        started = service.analysis(
            "start", project_id=project_id, plan_id=plan["plan_id"]
        )
        run_id = started["run"]["run_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            run = service.analysis("status", project_id=project_id, run_id=run_id)[
                "run"
            ]
            if run["status"] in {"succeeded", "failed", "cancelled", "blocked"}:
                break
            time.sleep(0.1)
        else:
            pytest.fail("real OCCT run did not reach a terminal state")
        assert run["status"] == "succeeded", run.get("error")
        assert {item["kind"] for item in run["artifacts"]} >= {
            "preflight",
            "topology_map",
            "render_scene",
            "features",
            "measurements",
            "scalar_field",
            "evaluations",
            "dfm_viewer",
        }
        jsonschema = pytest.importorskip("jsonschema")
        schema_root = Path("dfm-geometry/schemas").resolve()
        schema_by_kind = {
            "preflight": "preflight.schema.json",
            "topology_map": "topology_map.schema.json",
            "render_scene": "render_scene.schema.json",
            "features": "features.schema.json",
            "measurements": "measurements.schema.json",
            "scalar_field": "scalar_field.schema.json",
            "worker_result": "result.schema.json",
        }
        project_dir = service.workspace.project_dir(project_id)
        measurement_artifact = next(
            item for item in run["artifacts"] if item["kind"] == "measurements"
        )
        measurement_payload = json.loads(
            (project_dir / measurement_artifact["relative_path"]).read_text(
                encoding="utf-8"
            )
        )
        measurements = {
            item["quantity_id"]: item for item in measurement_payload["measurements"]
        }
        assert measurements["thickness_mm"]["method"] == "freecad_dfm_rolling_sphere"
        assert measurements["average_thickness_mm"]["method"] == (
            "freecad_dfm_rolling_sphere"
        )
        assert (
            measurements["average_thickness_mm"]["value"]
            >= (measurements["thickness_mm"]["value"])
        )
        assert (
            measurements["average_thickness_mm"]["diagnostics"]["material_independent"]
            is True
        )
        for artifact in run["artifacts"]:
            schema_name = schema_by_kind.get(artifact["kind"])
            if schema_name is None:
                continue
            schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
            payload = json.loads(
                (project_dir / artifact["relative_path"]).read_text(encoding="utf-8")
            )
            jsonschema.Draft202012Validator(schema).validate(payload)
        status = service.project("status", project_id=project_id)
        assert status["project"]["features"]
    finally:
        service.close()
        reset_hermes_home_override(token)
