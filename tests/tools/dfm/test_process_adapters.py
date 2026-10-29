from pathlib import Path

import pytest

from tools.dfm.analyzers.base import AnalyzerContext
from tools.dfm.errors import DFMError
from tools.dfm.runtime.process import SESSION_SCHEMA_BUNDLE_VERSION
from tools.dfm.processes.registry import build_default_process_registry
from tools.dfm.processes.base import ProcessPlan
from tools.dfm.processes.occt_injection import (
    compile_occt_injection_plan,
    geometry_binding_index,
)


@pytest.fixture
def context(tmp_path):
    return AnalyzerContext(
        project_id="dfm_project",
        project_dir=Path(tmp_path),
        input_mode="step",
        inputs=[],
    )


def test_default_process_registry_supports_injection_and_die_casting():
    registry = build_default_process_registry()

    assert registry.keys() == ("die_casting", "injection")
    with pytest.raises(DFMError) as exc_info:
        registry.get("machining")

    assert exc_info.value.code == "unsupported_capability"
    assert exc_info.value.details["supported_processes"] == ["die_casting", "injection"]


def test_injection_startup_requirements_do_not_depend_on_ontology_rows(monkeypatch):
    adapter = build_default_process_registry().get("injection")
    published = adapter.ontology_store.fact_requirements
    monkeypatch.setattr(
        adapter.ontology_store,
        "fact_requirements",
        lambda process: [
            item
            for item in published(process)
            if item["name"] not in {"model_units", "pull_dir"}
        ],
    )

    requirements = adapter.fact_requirements()

    assert [item.name for item in requirements[:3]] == [
        "process",
        "model_units",
        "pull_dir",
    ]
    assert all(item.phase == "discovery" for item in requirements[:3])


def test_injection_factor_requirement_preserves_impact_scope(monkeypatch):
    adapter = build_default_process_registry().get("injection")
    monkeypatch.setattr(
        adapter.ontology_store,
        "fact_requirements",
        lambda process: ({
            "name": "surface_finish",
            "question": "请确认表面状态要求。",
            "phase": "analysis",
            "required_by": ["C_WALL_DRAFT"],
            "check_ids": ["C_WALL_DRAFT"],
            "impact_scope": "特征实例级",
        },),
    )

    requirement = next(
        item for item in adapter.fact_requirements() if item.name == "surface_finish"
    )

    assert requirement.impact_scope == "特征实例级"


def test_die_casting_scope_is_independent_and_topology_only(context):
    adapter = build_default_process_registry().get("die_casting")

    plan = adapter.compile(context, {})

    assert plan.process == "die_casting"
    assert plan.scope_id == "die_casting.topology-baseline"
    assert plan.scope_version == "1.0.0"
    assert [item.calculator_id for item in plan.operations] == [
        "load_geometry",
        "inspect_topology",
    ]
    assert tuple(adapter.required_facts()) == ("process", "model_units")


def test_injection_plan_uses_published_ontology_and_capability_provenance(context):
    adapter = build_default_process_registry().get("injection")

    plan = adapter.compile(context, {})

    assert plan.process == "injection"
    assert plan.scope_id == "injection.default"
    assert plan.scope_version == adapter.ontology_store.identity().scope_version
    assert plan.adapter_version == "injection-geometric-v2"
    assert plan.ontology_snapshot_id == adapter.ontology_store.identity().snapshot_id
    assert len(plan.ontology_snapshot_sha256) == 64
    assert plan.rules["R_INJ_MAIN_WALL_MIN_ABS"].value == 1.2
    assert plan.rules["R_INJ_MAIN_WALL_MIN_ABS"].source.startswith(
        f"ontology:{plan.ontology_snapshot_id}/"
    )
    assert plan.rules["R_INJ_MAIN_WALL_DRAFT_DEFAULT"].value == 1.0
    assert plan.rules["R_INJ_MAIN_WALL_DRAFT_DEFAULT"].unit == "degree"
    assert plan.operations == []
    assert {item.geometric_id for item in plan.rule_bindings} == {
        "G_WALL_THK_MIN",
        "G_WALL_IN_DRAFT",
    }
    wall_binding = next(
        item for item in plan.rule_bindings if item.geometric_id == "G_WALL_THK_MIN"
    )
    assert wall_binding.required_fact_names == ["material"]
    requirements = {item.name: item for item in adapter.fact_requirements()}
    assert requirements["process"].phase == "discovery"
    assert requirements["model_units"].phase == "discovery"
    assert requirements["material"].required_by == (
        "check.main_wall_minimum_thickness",
    )
    assert requirements["material"].check_ids == (
        "check.main_wall_minimum_thickness",
    )
    assert requirements["pull_dir"].required_by == ("geometry.draft",)
    assert requirements["pull_dir"].check_ids == ("check.main_wall_minimum_draft",)
    payloads = [item.to_dict() for item in plan.rule_bindings]
    keys = {
        key
        for payload in payloads
        for key in payload
    }
    for forbidden in (
        "worker_geometric_id",
        "operation_id",
        "calculator_id",
        "metric_id",
        "quantity_id",
        "region_refs",
        "face_indices",
        "task_id",
    ):
        assert forbidden not in keys


def test_confirmed_geometry_fact_is_normalized_and_traced(context):
    adapter = build_default_process_registry().get("injection")

    plan = adapter.compile(
        context,
        {
            "pull_dir": {
                "value": [0, 1, 0],
                "source": "user_confirmed",
                "source_ref": "fact:fact_pull_direction",
            },
        },
    )

    assert plan.rules["R_INJ_MAIN_WALL_MIN_ABS"].value == 1.2
    assert plan.operations == []
    assert adapter.normalize_parameter("pull_dir", [0, 1, 0]) == [0.0, 1.0, 0.0]


def test_occt_plan_does_not_restore_raw_pull_direction_after_adapter_normalization(
    context,
):
    adapter = build_default_process_registry().get("injection")

    plan = compile_occt_injection_plan(
        adapter,
        context,
        {
            "model_units": {
                "value": "mm",
                "source": "project_fact",
                "source_ref": "fact:fact_model_units",
            },
            "pull_dir": {
                "value": "Z+",
                "source": "project_fact",
                "source_ref": "fact:fact_pull_direction",
            },
        },
        _occt_geometric_scope(),
    )

    assert plan.operations == []
    assert all(
        not operand.context
        for binding in plan.rule_bindings
        for operand in binding.measurement_operands()
    )


def test_material_profile_changes_hermes_rule_without_entering_backend_arguments(
    context,
):
    adapter = build_default_process_registry().get("injection")

    plan = adapter.compile(context, {"material": "ABS", "model_units": "mm"})

    assert plan.rules["R_INJ_MAIN_WALL_MIN_ABS"].value == 1.2
    assert all("material" not in item.arguments for item in plan.operations)


def test_project_facts_cannot_override_a_published_rule_threshold(context):
    adapter = build_default_process_registry().get("injection")

    with pytest.raises(DFMError) as exc_info:
        adapter.compile(context, {"min_wall_mm": 1.6})

    assert exc_info.value.code == "process_parameter_invalid"


@pytest.mark.parametrize("model_units", ["inch", "cm", "unknown"])
def test_frozen_scope_rejects_unsupported_model_units(context, model_units):
    adapter = build_default_process_registry().get("injection")

    with pytest.raises(DFMError) as exc_info:
        adapter.compile(context, {"model_units": model_units})

    assert exc_info.value.code == "process_parameter_invalid"


@pytest.mark.parametrize(
    "parameters",
    [
        {"imaginary_threshold": 1},
        {"min_wall_mm": 0},
        {"pull_dir": [0, 0]},
        {"min_draft_deg": {"value": 1, "source": "model_guess"}},
    ],
)
def test_injection_adapter_rejects_unknown_or_untrusted_parameters(context, parameters):
    adapter = build_default_process_registry().get("injection")

    with pytest.raises(DFMError) as exc_info:
        adapter.compile(context, parameters)

    assert exc_info.value.code == "process_parameter_invalid"


def _occt_geometric_scope():
    return {
        "contract_version": "dfm.geometry/v2",
        "schema_bundle_version": SESSION_SCHEMA_BUNDLE_VERSION,
        "geometrics": [{
            "geometric_id": "G_WALL_THK_MIN",
            "support_status": "supported",
            "feature_kind": "main_wall",
            "scope_modes": ["feature"],
            "direction_modes": ["none"],
            "requires_complete_inventory": [],
            "requires_relationships": [],
            "context_schema": {},
        }],
    }


def test_occt_scope_projects_worker_binding_from_geometric_id_only():
    binding = geometry_binding_index(_occt_geometric_scope())["G_WALL_THK_MIN"]

    assert binding == {
        "feature_kind": "main_wall",
        "scope_modes": ["feature"],
        "direction_modes": ["none"],
        "requires_complete_inventory": [],
        "requires_relationships": [],
        "context_schema": {},
    }


@pytest.mark.parametrize("requirements", [None, True, ["adjacent_main_wall"], [{"type": "adjacent_main_wall"}],
    [{"type": "adjacent_main_wall", "target_feature_kind": "main_wall"}] * 2])
def test_occt_reference_requirements_must_be_explicit_unique_descriptors(requirements):
    scope = _occt_geometric_scope()
    scope["geometrics"][0]["requires_relationships"] = requirements
    with pytest.raises(DFMError) as error:
        geometry_binding_index(scope)
    assert error.value.code == "process_scope_invalid"


def test_occt_plan_drops_operations_when_no_rule_binding_is_executable(context):
    class NoRuleAdapter:
        class ontology_store:
            @staticmethod
            def configure_geometric_bindings(_bindings):
                return None

        @staticmethod
        def normalize_parameter(_name, value):
            return value

        @staticmethod
        def compile(_context, _parameters):
            return ProcessPlan(
                process="injection",
                adapter_version="test",
                scope_id="test",
                scope_version="1",
                rules={},
                operations=[],
                accepted_inputs=set(),
                rule_bindings=[],
            )

    plan = compile_occt_injection_plan(
        NoRuleAdapter(), context, {}, _occt_geometric_scope()
    )

    assert plan.operations == []
    assert plan.rule_bindings == []


def test_occt_supported_binding_requires_available_runtime_operations():
    scope = _occt_geometric_scope()
    scope["geometrics"][0]["scope_modes"] = []

    with pytest.raises(DFMError) as exc_info:
        geometry_binding_index(scope)

    assert exc_info.value.code == "process_scope_invalid"


def test_occt_inventory_dependencies_name_the_required_feature_kinds():
    scope = _occt_geometric_scope()
    scope["geometrics"][0]["requires_complete_inventory"] = ["rib", "drilled_hole"]
    assert geometry_binding_index(scope)["G_WALL_THK_MIN"]["requires_complete_inventory"] == ["rib", "drilled_hole"]
    scope["geometrics"][0]["requires_complete_inventory"] = True
    with pytest.raises(DFMError):
        geometry_binding_index(scope)
