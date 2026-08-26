from pathlib import Path
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.tools.dfm.baseline import occ_available
from tools.dfm.service import DFMService


FIXTURE = Path("tests/fixtures/dfm/step/injection_plate_with_hole.step").resolve()


@pytest.mark.skipif(not occ_available(), reason="real M2.5 E2E requires pythonocc-core")
def test_die_casting_step_topology_vertical_slice(tmp_path):
    token = set_hermes_home_override(tmp_path / "home")
    service = DFMService(reconcile_jobs=False)
    try:
        project_id = service.project("create", name="M2.5 die casting E2E")["project_id"]
        service.project("add_input", project_id=project_id, path=str(FIXTURE))
        blocked = service.analysis("discover", project_id=project_id, process="die_casting")
        assert [item["clarification_id"] for item in blocked["clarifications"]] == ["clarification_model_units"]
        service.project("confirm_fact", project_id=project_id, fact_name="model_units", fact_value="mm")
        service.analysis("discover", project_id=project_id, process="die_casting")
        plan = service.analysis("plan", project_id=project_id, process="die_casting")
        assert plan["plan"]["scope_id"] == "die_casting.topology-baseline"
        started = service.analysis("start", project_id=project_id, plan_id=plan["plan"]["plan_id"])
        run_id = started["run"]["run_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            run = service.analysis("status", project_id=project_id, run_id=run_id)["run"]
            if run["status"] in {"succeeded", "failed", "blocked", "cancelled"}:
                break
            time.sleep(0.1)
        else:
            pytest.fail("M2.5 die-casting run did not reach a terminal state")

        assert run["status"] == "succeeded", run.get("error")
        assert run["plan_snapshot"]["process"] == "die_casting"
        assert {item["kind"] for item in run["artifacts"]} >= {
            "measurements", "report_json", "report_markdown", "report_presentation", "worker_result"
        }
    finally:
        service.close()
        reset_hermes_home_override(token)
