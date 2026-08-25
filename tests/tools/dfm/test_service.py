from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.drawing import DrawingAnalyzer
from tools.dfm.analyzers.fusion import FusionAnalyzer
from tools.dfm.analyzers.occt import (
    ENGINE_VERSION,
    GEOMETRY_OPERATION_PAIRS,
    OcctAnalyzer,
)
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.contracts import FeatureRecord, GeometryRef, RegionRecord
from tools.dfm.errors import DFMError
from tools.dfm.service import DFMService


STEP_PAYLOAD = (
    Path(__file__).parents[3] / "tests" / "fixtures" / "dfm" / "step" / "injection_plate_with_hole.step"
).read_bytes()

OCCT_CAPABILITIES = {
    "contract_version": "dfm.geometry.capabilities/v1",
    "engine_version": ENGINE_VERSION,
    "backend": "analysis_situs+occt",
    "analysis_situs_version": "v2025.2",
    "analysis_situs_commit": "aa5958932c8c85c068566ab685f2b99c0436b926",
    "occt_version": "7.9.3",
    "status": "available",
    "maturity": "experimental",
    "supported_processes": ["injection"],
    "supported_formats": ["step"],
    "supported_extensions": [".step", ".stp"],
    "output_artifact_kinds": [
        "preflight",
        "topology_map",
        "render_mesh",
        "features",
        "measurements",
        "metric_fields",
    ],
    "operations": [
        {
            "operation_id": operation_id,
            "calculator_id": calculator_id,
            "maturity": "experimental",
            "algorithm_version": ENGINE_VERSION,
        }
        for operation_id, calculator_id in GEOMETRY_OPERATION_PAIRS
    ],
}


def confirm_step_facts(dfm, project_id):
    for name, value in {
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
    registry.register(
        OcctAnalyzer(
            "C:/dfm/dfm-geometry.exe",
            capability_probe=lambda _: OCCT_CAPABILITIES,
        )
    )
    registry.register(DrawingAnalyzer())
    registry.register(FusionAnalyzer())
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
        fact_name="model_units",
        fact_value="mm",
    )
    status = dfm.project("status", project_id=created["project_id"])
    listed = dfm.project("list")

    assert added["input"]["kind"] == "step"
    assert {item["clarification_id"] for item in added["open_clarifications"]} == {
        "clarification_model_units",
    }
    assert confirmed["fact"]["status"] == "confirmed"
    assert status["project"]["input_mode"] == "step"
    assert next(
        item
        for item in status["project"]["clarifications"]
        if item["clarification_id"] == "clarification_model_units"
    ) == {
        "clarification_id": "clarification_model_units",
        "question": "What length unit was used to author the STEP model?",
        "status": "answered",
        "answer": "mm",
    }
    assert status["project"]["open_clarifications"] == []
    assert status["capabilities"]["occt"]["status"] == "available"
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


def test_inch_step_unit_is_preserved_in_occt_plan_for_native_normalization(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Inch-authored part")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="model_units", fact_value="inch"
    )
    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="material", fact_value="ABS"
    )
    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="pull_dir", fact_value=[0, 0, 1]
    )
    dfm.analysis("discover", project_id=project_id)

    plan = dfm.analysis(
        "plan", project_id=project_id, verification_level="experimental"
    )["plan"]

    preflight = next(
        item for item in plan["operations"] if item["operation_id"] == "geometry.preflight"
    )
    assert preflight["arguments"]["model_unit"]["value"] == "inch"
    assert plan["assumed_pull_direction"] is False


def test_material_fact_selects_rules_without_being_forwarded_to_occt(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Legacy material project")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="material", fact_value="ABS"
    )
    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="model_units", fact_value="mm"
    )
    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="pull_dir", fact_value=[0, 0, 1]
    )
    dfm.analysis("discover", project_id=project_id)

    plan = dfm.analysis(
        "plan", project_id=project_id, verification_level="experimental"
    )["plan"]

    assert plan["status"] == "ready"
    assert all(
        "material" not in operation["arguments"] for operation in plan["operations"]
    )


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

    discovery_blocked = dfm.analysis("discover", project_id=project_id)
    assert discovery_blocked["status"] == "clarification_required"
    assert discovery_blocked["requires_user_response"] is True
    assert discovery_blocked["do_not_infer"] is True
    confirm_step_facts(dfm, project_id)
    discovery = dfm.analysis("discover", project_id=project_id)
    assert discovery["snapshot"]["status"] == "frozen"
    plan = dfm.analysis("plan", project_id=project_id)

    assert plan["plan"]["analyzer_keys"] == ["occt"]
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
    assert plan["plan"]["verification_level"] == "certified"
    assert plan["capability"]["status"] == "disabled"
    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id, plan_id=plan["plan"]["plan_id"])
    assert exc_info.value.code == "verification_unavailable"

    opted_in = dfm.analysis(
        "plan",
        project_id=project_id,
        verification_level="experimental",
    )
    assert opted_in["plan"]["status"] == "ready"
    assert opted_in["plan"]["verification_level"] == "experimental"
    assert opted_in["capability"]["status"] == "available"


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
        fact_name="model_units",
        fact_value="inch",
    )

    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id, plan_id=plan["plan_id"])
    assert exc_info.value.code == "plan_not_ready"
    assert exc_info.value.details["status"] == "invalidated"


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
    assert [item["operation_id"] for item in rebuilt["operations"]] == [
        item["operation_id"] for item in plan["operations"]
    ]
    assert [item["calculator_id"] for item in rebuilt["operations"]] == [
        item["calculator_id"] for item in plan["operations"]
    ]
    assert {
        ref
        for item in rebuilt["operations"]
        for ref in item["region_refs"]
    } != {
        ref for item in plan["operations"] for ref in item["region_refs"]
    }


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

    assert [item["operation_id"] for item in rebuilt["operations"]] == [
        "geometry.preflight",
        "topology.index",
        "topology.aag",
        "measure_draft",
    ]
    assert [item["quantity_id"] for item in rebuilt["rule_bindings"]] == [
        "draft_angle_deg",
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
        "geometry_preflight",
        "index_topology",
        "build_aag",
        "measure_wall_thickness",
    ]
    assert rebuilt["operations"][3]["operation_id"] == "measure_wall_thickness"
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
    dfm.analysis("discover", project_id=project_id, process="die_casting")
    result = dfm.analysis("plan", project_id=project_id, process="die_casting")
    status = dfm.project("status", project_id=project_id)

    assert result["plan"]["process"] == "die_casting"
    assert result["plan"]["scope_id"] == "die_casting.topology-baseline"
    assert [item["calculator_id"] for item in result["plan"]["operations"]] == [
        "load_geometry",
        "inspect_topology",
    ]
    assert result["plan"]["status"] == "blocked"
    assert result["capability"]["error_code"] == "unsupported_capability"
    assert status["project"]["process"] == "die_casting"
    assert status["project"]["process_source"] == "user_selected"


def test_new_parasolid_input_is_rejected(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Retired Parasolid input")["project_id"]
    source = temp / "part.x_t"
    source.write_text("Parasolid transmit text file\nbody data\n", encoding="ascii")
    with pytest.raises(DFMError) as exc_info:
        dfm.project("add_input", project_id=project_id, path=str(source))
    assert exc_info.value.code == "input_type_unsupported"
