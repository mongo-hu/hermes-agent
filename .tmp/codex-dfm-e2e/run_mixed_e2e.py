from __future__ import annotations

import json
from pathlib import Path
import time

from tools.dfm.config import DFMConfig
from tools.dfm.errors import DFMError
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.service import DFMService


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = Path(__file__).resolve().parent / "workspace"
STEP = ROOT / "tests/fixtures/dfm/step/injection_plate_with_hole.step"
DRAWING = ROOT / "docs/assets/dfm-system-architecture-ppt.png"


def main() -> None:
    service = DFMService(
        config=DFMConfig(max_concurrent_runs=1, drawing_model="gpt-4o"),
        workspace=DFMWorkspace(WORKSPACE),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="Mixed STEP and drawing E2E")[
            "project_id"
        ]
        step_input = service.project(
            "add_input", project_id=project_id, path=str(STEP)
        )["input"]
        drawing_input = service.project(
            "add_input", project_id=project_id, path=str(DRAWING)
        )["input"]
        for name, value in {
            "process": "injection",
            "model_units": "mm",
        }.items():
            service.project(
                "confirm_fact",
                project_id=project_id,
                fact_name=name,
                fact_value=value,
            )

        discovery = service.analysis("discover", project_id=project_id)

        for name, value in {
            "material": "ABS",
            "pull_dir": [0, 0, 1],
        }.items():
            service.project(
                "confirm_fact",
                project_id=project_id,
                fact_name=name,
                fact_value=value,
            )
        plan = service.analysis("plan", project_id=project_id)
        start_error = None
        result = None
        try:
            started = service.analysis(
                "start", project_id=project_id, plan_id=plan["plan"]["plan_id"]
            )
            run_id = started["run"]["run_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                run = service.analysis(
                    "status", project_id=project_id, run_id=run_id
                )["run"]
                if run["status"] in {
                    "succeeded",
                    "failed",
                    "blocked",
                    "cancelled",
                }:
                    break
                time.sleep(0.1)
            result = service.analysis(
                "result", project_id=project_id, run_id=run_id
            )["run"]
        except DFMError as exc:
            start_error = exc.to_dict()
        print(
            json.dumps(
                {
                    "project_id": project_id,
                    "input_mode": plan["plan"]["input_mode"],
                    "step_input": step_input["input_id"],
                    "drawing_input": drawing_input["input_id"],
                    "drawing_discovery": discovery.get("drawing_discovery"),
                    "observation_count": len(discovery.get("observations", [])),
                    "fusion_link_count": len(discovery.get("fusion_links", [])),
                    "plan_analyzers": plan["plan"]["analyzer_keys"],
                    "plan_capability": plan["capability"],
                    "start_error": start_error,
                    "run_status": result["status"] if result else None,
                    "run_error": result.get("error") if result else None,
                    "artifact_kinds": [
                        artifact["kind"]
                        for artifact in (result or {}).get("artifacts", [])
                    ],
                    "workspace": str(WORKSPACE),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        service.close()


if __name__ == "__main__":
    main()
