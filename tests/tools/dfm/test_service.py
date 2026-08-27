from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.drawing import DrawingAnalyzer
from tools.dfm.analyzers.fusion import FusionAnalyzer
from tools.dfm.analyzers.parasolid import ParasolidAnalyzer
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.analyzers.step import StepAnalyzer
from tools.dfm.errors import DFMError
from tools.dfm.service import DFMService
from tools.dfm.contracts import (
    ClarificationRecord,
    FeatureRecord,
    GeometryRef,
    ObservationRecord,
    RegionRecord,
)


STEP_PAYLOAD = (
    Path(__file__).parents[3] / "tests" / "fixtures" / "dfm" / "step" / "injection_plate_with_hole.step"
).read_bytes()


def confirm_step_facts(dfm, project_id):
    for name, value in {
        "process": "injection",
        "material": "ABS",
        "pull_dir": [0, 0, 1],
        "model_units": "mm",
    }.items():
        dfm.project(
            "confirm_fact", project_id=project_id, fact_name=name, fact_value=value
        )


@pytest.fixture
def service(tmp_path):
    token = set_hermes_home_override(tmp_path / "home")
    registry = AnalyzerRegistry()
    registry.register(StepAnalyzer(dependency_probe=lambda: False))
    registry.register(DrawingAnalyzer())
    registry.register(FusionAnalyzer())
    registry.register(ParasolidAnalyzer())
    instance = DFMService(registry=registry, reconcile_jobs=False)
    try:
        yield instance, tmp_path
    finally:
        instance.close()
        reset_hermes_home_override(token)


def test_project_actions_create_add_input_status_confirm_and_list(service):
    dfm, temp = service
    created = dfm.project("create", name="Bracket", idempotency_key="create-1")
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)

    added = dfm.project("add_input", project_id=created["project_id"], path=str(source))
    confirmed = dfm.project(
        "confirm_fact",
        project_id=created["project_id"],
        fact_name="material",
        fact_value="ABS",
    )
    status = dfm.project("status", project_id=created["project_id"])
    listed = dfm.project("list")

    assert added["input"]["kind"] == "step"
    assert {item["clarification_id"] for item in added["open_clarifications"]} == {
        "clarification_process",
        "clarification_model_units",
    }
    assert confirmed["fact"]["status"] == "confirmed"
    assert status["project"]["input_mode"] == "step"
    assert next(
        item for item in status["project"]["facts"] if item["name"] == "material"
    )["value"] == "ABS"
    assert {item["clarification_id"] for item in status["project"]["open_clarifications"]} == {
        "clarification_process",
        "clarification_model_units",
    }
    assert status["capabilities"]["step"]["status"] == "dependency_missing"
    assert listed["projects"][0]["project_id"] == created["project_id"]


def test_analysis_context_exposes_bounded_ontology_to_the_agent(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Ontology context")["project_id"]
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="material",
        fact_value="ABS",
    )

    result = dfm.analysis(
        "context",
        project_id=project_id,
        check_id="check.main_wall_minimum_thickness",
    )

    assert result["confirmed_facts"]["material"]["value"] == "ABS"
    assert len(result["checks"]) == 1
    context = result["checks"][0]
    assert context["check"]["concept_id"] == "check.main_wall_minimum_thickness"
    assert context["rules"][0]["rule_id"] == "R_INJ_MAIN_WALL_MIN_ABS"
    assert any(item["predicate"] == "USES_OPERAND" for item in context["relations"])


def test_factor_source_policy_auto_accepts_project_metadata_observation(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Factor resolver")["project_id"]
    dfm._store(project_id).update(
        lambda current: replace(
            current,
            observations=[
                ObservationRecord(
                    observation_id="observation.material.metadata",
                    input_id="project-metadata",
                    kind="material",
                    value="ABS",
                    source_refs=["project-metadata:material@1"],
                    confidence=1.0,
                    provenance={"source": "project_metadata"},
                )
            ],
        )
    )

    dfm.analysis("discover", project_id=project_id)
    project = dfm.project("status", project_id=project_id)["project"]

    fact = next(item for item in project["facts"] if item["name"] == "material")
    observation = project["observations"][0]
    assert fact["status"] == "confirmed"
    assert fact["source"] == "project_metadata"
    assert fact["evidence_refs"] == ["project-metadata:material@1"]
    assert observation["status"] == "accepted"


def test_factor_source_policy_keeps_recognition_as_pending_observation(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Factor confirmation")["project_id"]
    dfm._store(project_id).update(
        lambda current: replace(
            current,
            observations=[
                ObservationRecord(
                    observation_id="observation.material.drawing",
                    input_id="drawing-1",
                    kind="material",
                    value="ABS",
                    source_refs=["drawing:drawing-1/annotation/7@1"],
                    confidence=0.95,
                    provenance={"source": "drawing_recognition"},
                )
            ],
        )
    )

    dfm.analysis("discover", project_id=project_id)
    project = dfm.project("status", project_id=project_id)["project"]

    assert not any(item["name"] == "material" for item in project["facts"])
    assert project["observations"][0]["status"] == "needs_confirmation"
    clarification = next(
        item
        for item in project["clarifications"]
        if item["clarification_id"] == "clarification_material"
    )
    assert clarification["status"] == "open"


def test_analysis_context_requires_one_check_id(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Bounded ontology context")["project_id"]

    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("context", project_id=project_id)

    assert exc_info.value.code == "ontology_check_required"
    assert exc_info.value.details["available_check_ids"]


def test_fact_alias_units_closes_model_units_clarification(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))

    confirmed = dfm.project(
        "confirm_fact", project_id=project_id, fact_name="units", fact_value="mm"
    )
    status = dfm.project("status", project_id=project_id)

    assert confirmed["fact"]["name"] == "model_units"
    assert confirmed["fact"]["value"] == "mm"
    assert "clarification_model_units" not in {
        item["clarification_id"] for item in status["project"]["open_clarifications"]
    }
    row = next(
        item
        for item in status["project"]["clarifications"]
        if item["clarification_id"] == "clarification_model_units"
    )
    assert row["status"] == "answered"


def test_legacy_model_length_unit_clarification_is_reconciled(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Legacy unit clarification")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    dfm._store(project_id).update(
        lambda current: replace(
            current,
            clarifications=[
                replace(
                    item,
                    clarification_id="clarification_model_length_unit",
                )
                if item.clarification_id == "clarification_model_units"
                else item
                for item in current.clarifications
            ],
        )
    )

    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="model_units",
        fact_value="mm",
    )
    project = dfm.project("status", project_id=project_id)["project"]

    row = next(
        item
        for item in project["clarifications"]
        if item["clarification_id"] == "clarification_model_length_unit"
    )
    assert row["status"] == "answered"
    assert row["answer"] == "mm"
    assert "clarification_model_length_unit" not in {
        item["clarification_id"] for item in project["open_clarifications"]
    }


def test_legacy_tool_main_draw_dir_clarification_uses_pull_dir(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Legacy pull direction")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    dfm._store(project_id).update(
        lambda current: replace(
            current,
            clarifications=[
                *current.clarifications,
                ClarificationRecord(
                    "clarification_tool_main_draw_dir",
                    "Confirm the main mold draw direction.",
                    "open",
                ),
            ],
        )
    )

    confirmed = dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="pull_dir",
        fact_value=[0, 0, 1],
    )
    project = dfm.project("status", project_id=project_id)["project"]

    assert confirmed["fact"]["name"] == "pull_dir"
    row = next(
        item
        for item in project["clarifications"]
        if item["clarification_id"] == "clarification_tool_main_draw_dir"
    )
    assert row["status"] == "answered"
    assert row["answer"] == [0, 0, 1]


def test_missing_run_id_is_recovered_only_when_unambiguous():
    one = SimpleNamespace(run_id="run_only", status="running")
    manifest = SimpleNamespace(runs=[one])
    assert DFMService._resolve_run_id(manifest, None, "status") == "run_only"

    many = SimpleNamespace(
        runs=[
            SimpleNamespace(run_id="run_a", status="succeeded"),
            SimpleNamespace(run_id="run_b", status="running"),
        ]
    )
    assert DFMService._resolve_run_id(many, None, "status") == "run_b"

    ambiguous = SimpleNamespace(
        runs=[
            SimpleNamespace(run_id="run_a", status="running"),
            SimpleNamespace(run_id="run_b", status="running"),
        ]
    )
    with pytest.raises(DFMError) as exc_info:
        DFMService._resolve_run_id(ambiguous, None, "status")
    assert exc_info.value.code == "run_id_required"


def test_plan_is_persisted_but_unavailable_production_start_fails_explicitly(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    added = dfm.project("add_input", project_id=project_id, path=str(source))

    blocked = dfm.analysis("plan", project_id=project_id)
    assert blocked["status"] == "discovery_required"
    assert blocked["next_action"] == "discover"
    confirm_step_facts(dfm, project_id)
    discovery = dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)

    assert discovery["snapshot"]["feature_refs"]
    assert discovery["features"][0]["kind"] == "ordinary_part"
    assert discovery["regions"][0]["mode"] == "whole_model"
    assert discovery["capability"]["providers"]["occt_cpp_feature_recognition"] == (
        "external-contract-1:not_implemented"
    )
    assert plan["plan"]["analyzer_keys"] == ["step"]
    assert plan["plan"]["discovery_snapshot_refs"] == [
        discovery["snapshot"]["snapshot_id"]
    ]
    measured = [item for item in plan["plan"]["operations"] if item["metric_ids"]]
    assert all(item["feature_refs"] == discovery["snapshot"]["feature_refs"] for item in measured)
    assert all(item["region_refs"] == discovery["snapshot"]["region_refs"] for item in measured)
    assert plan["plan"]["process"] == "injection"
    assert plan["plan"]["scope_id"] == "injection.default"
    assert plan["plan"]["scope_version"] == "1.1.0"
    assert plan["plan"]["ontology_snapshot_id"] == (
        "ontology.injection.default@1.1.0"
    )
    assert len(plan["plan"]["ontology_snapshot_sha256"]) == 64
    assert plan["plan"]["input_ids"] == [plan["plan"]["input_ids"][0]]
    assert set(plan["plan"]["input_hashes"].values()) == {added["input"]["sha256"]}
    draft_rule = plan["plan"]["rules"]["R_INJ_MAIN_WALL_DRAFT_DEFAULT"]
    assert draft_rule["value"] == 1.0
    assert draft_rule["unit"] == "degree"
    assert draft_rule["version"] == "1.0.0"
    assert draft_rule["source"].startswith(
        "ontology:ontology.injection.default@1.1.0/"
    )
    assert plan["capability"]["status"] == "dependency_missing"
    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id, plan_id=plan["plan"]["plan_id"])
    assert exc_info.value.code == "dependency_missing"


def test_input_or_confirmed_fact_invalidates_prior_plan(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    confirm_step_facts(dfm, project_id)
    dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)["plan"]

    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="material",
        fact_value="PC",
    )

    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id, plan_id=plan["plan_id"])
    assert exc_info.value.code == "plan_not_ready"
    assert exc_info.value.details["status"] == "invalidated"


def test_start_requires_explicit_plan_id(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Missing plan id")["project_id"]

    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id)

    assert exc_info.value.code == "plan_id_required"
    assert exc_info.value.details == {"action": "start"}


def test_new_input_version_supersedes_prior_input_and_replans_full_scope(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    first = dfm.project("add_input", project_id=project_id, path=str(source))["input"]
    confirm_step_facts(dfm, project_id)
    dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)["plan"]

    source.write_bytes(STEP_PAYLOAD + b"\n/* revised */\n")
    second = dfm.project("add_input", project_id=project_id, path=str(source))["input"]
    dfm.analysis("discover", project_id=project_id)
    rebuilt = dfm.analysis(
        "plan", project_id=project_id, base_plan_id=plan["plan_id"]
    )["plan"]

    assert second["supersedes_input_id"] == first["input_id"]
    assert rebuilt["parent_plan_id"] == plan["plan_id"]
    assert rebuilt["input_ids"] == [second["input_id"]]
    assert [item["calculator_id"] for item in rebuilt["operations"]] == [
        item["calculator_id"] for item in plan["operations"]
    ]
    assert rebuilt["operations"][2]["region_refs"] != plan["operations"][2][
        "region_refs"
    ]


def test_pull_direction_rebuild_only_includes_affected_operation_closure(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    confirm_step_facts(dfm, project_id)
    dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)["plan"]

    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="pull_dir", fact_value=[1, 0, 0]
    )
    rebuilt = dfm.analysis(
        "plan", project_id=project_id, base_plan_id=plan["plan_id"]
    )["plan"]

    assert [item["calculator_id"] for item in rebuilt["operations"]] == [
        "load_geometry",
        "inspect_topology",
        "measure_draft",
    ]
    assert rebuilt["operations"][2]["operation_id"].startswith("geometry.draft.")
    assert [item["quantity_id"] for item in rebuilt["rule_bindings"]] == [
        "draft_angle_deg"
    ]


def test_material_rebuild_uses_wall_rule_dependency_without_draft_recalculation(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    confirm_step_facts(dfm, project_id)
    dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)["plan"]

    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="material", fact_value="ABS"
    )
    rebuilt = dfm.analysis(
        "plan", project_id=project_id, base_plan_id=plan["plan_id"]
    )["plan"]

    assert [item["calculator_id"] for item in rebuilt["operations"]] == [
        "load_geometry",
        "inspect_topology",
        "measure_wall_thickness",
    ]
    assert rebuilt["operations"][2]["operation_id"].startswith(
        "geometry.wall_thickness."
    )
    assert [item["quantity_id"] for item in rebuilt["rule_bindings"]] == [
        "thickness_mm"
    ]


def test_analysis_only_fact_change_reuses_discovery_snapshot(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    confirm_step_facts(dfm, project_id)
    first = dfm.analysis("discover", project_id=project_id)["snapshot"]

    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="material", fact_value="PC"
    )
    second = dfm.analysis("discover", project_id=project_id)["snapshot"]

    assert second["snapshot_id"] == first["snapshot_id"]
    facts = dfm.project("status", project_id=project_id)["project"]["facts"]
    assert set(second["confirmed_fact_refs"]) == {
        item["fact_id"]
        for item in facts
        if item["name"] in {"process", "model_units"}
    }


def test_unpublished_feature_region_remains_inside_ordinary_analysis_scope(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Feature-aware bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    added = dfm.project("add_input", project_id=project_id, path=str(source))["input"]
    confirm_step_facts(dfm, project_id)
    feature_id = "feature.screw_boss.001"
    region_id = "region.screw_boss.001.wall"
    face = GeometryRef("face", 1, added["sha256"], "topology_test", "face_000001")

    dfm._store(project_id).update(
        lambda current: __import__("dataclasses").replace(
            current,
            features=[
                *current.features,
                FeatureRecord(
                    feature_id,
                    "screw_boss",
                    ["recognizer:test"],
                    0.99,
                    input_sha256=added["sha256"],
                    region_refs=[region_id],
                    recognizer="test-recognizer",
                    recognizer_version="1",
                    status="confirmed",
                ),
            ],
            regions=[
                *current.regions,
                RegionRecord(
                    region_id,
                    added["sha256"],
                    "model",
                    "topology_refs",
                    "screw_boss_wall",
                    ["recognizer:test"],
                    "1",
                    "b" * 64,
                    geometry_refs=[face],
                    role="wall",
                    feature_refs=[feature_id],
                ),
            ],
        )
    )

    discovery = dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)["plan"]
    regions = {item["region_id"]: item for item in plan["regions"]}
    ordinary_id = next(
        item["region_id"] for item in discovery["regions"] if item["role"] == "ordinary"
    )

    assert regions[ordinary_id]["mode"] == "whole_model"
    assert regions[ordinary_id]["excluded_geometry_refs"] == []
    measured = [item for item in plan["operations"] if item["metric_ids"]]
    assert len(measured) == 2
    assert {(item["calculator_id"], item["region_refs"][0]) for item in measured} == {
        ("measure_wall_thickness", ordinary_id),
        ("measure_draft", ordinary_id),
    }
    assert len({item["operation_id"] for item in measured}) == 2


def test_desktop_file_reference_prefix_is_accepted(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = Path(temp / "part.step")
    source.write_bytes(STEP_PAYLOAD)

    result = dfm.project("add_input", project_id=project_id, path=f"@file:{source}")

    assert result["input"]["source_name"] == "part.step"


def test_die_casting_plan_uses_its_own_facts_scope_and_operations(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Die-cast housing")["project_id"]
    source = temp / "housing.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))

    blocked = dfm.analysis("discover", project_id=project_id, process="die_casting")

    assert [item["clarification_id"] for item in blocked["clarifications"]] == [
        "clarification_model_units"
    ]
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="model_units",
        fact_value="mm",
    )
    discovery = dfm.analysis("discover", project_id=project_id, process="die_casting")
    result = dfm.analysis("plan", project_id=project_id, process="die_casting")
    status = dfm.project("status", project_id=project_id)

    assert result["plan"]["process"] == "die_casting"
    assert result["plan"]["discovery_snapshot_refs"] == [
        discovery["snapshot"]["snapshot_id"]
    ]
    assert result["plan"]["scope_id"] == "die_casting.topology-baseline"
    assert [item["calculator_id"] for item in result["plan"]["operations"]] == [
        "load_geometry",
        "inspect_topology",
    ]
    assert status["project"]["process"] == "die_casting"
    assert status["project"]["process_source"] == "user_selected"


def test_parasolid_capability_is_local_and_does_not_disable_step(service):
    dfm, temp = service
    project_id = dfm.project("create", name="NX backend capability")["project_id"]
    source = temp / "part.x_t"
    source.write_text("Parasolid transmit text file\nbody data\n", encoding="ascii")
    dfm.project("add_input", project_id=project_id, path=str(source))

    status = dfm.project("status", project_id=project_id)

    assert status["project"]["inputs"][0]["format_id"] == "parasolid_xt"
    assert status["capabilities"]["parasolid"]["status"] == "dependency_missing"
    assert status["capabilities"]["step"]["status"] == "dependency_missing"
    dfm.analysis("discover", project_id=project_id)
    plan = dfm.analysis("plan", project_id=project_id)
    assert plan["plan"]["status"] == "blocked"
    assert plan["capability"]["status"] == "dependency_missing"
