from dataclasses import replace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.drawing import DrawingAnalyzer
from tools.dfm.analyzers.fusion import FusionAnalyzer
from tools.dfm.analyzers.parasolid import ParasolidAnalyzer
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.analyzers.step import StepAnalyzer
from tools.dfm.config import DFMConfig
from tools.dfm.errors import DFMError
from tools.dfm.processes.base import FactRequirement
from tools.dfm.service import DFMService
from tools.dfm.contracts import (
    ClarificationRecord,
    Capability,
    CapabilityStatus,
    EffectiveRule,
    FactRecord,
    FeatureRecord,
    GeometricRequest,
    GeometricRuleBinding,
    FormingScopeRecord,
    GeometryRef,
    ObservationRecord,
    PlanFragmentRecord,
    PlanRecord,
    ReviewGroupRecord,
)
from tools.dfm.processes.base import ProcessPlan
from tools.dfm.processes.occt_injection import geometry_binding_index


STEP_PAYLOAD = (
    Path(__file__).parents[3]
    / "tests"
    / "fixtures"
    / "dfm"
    / "step"
    / "injection_plate_with_hole.step"
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
    instance = DFMService(
        config=DFMConfig(
            geometry_backend="pythonocc_internal",
            geometry_executable=str(tmp_path / "missing-dfm-geometry.exe"),
        ),
        registry=registry,
        reconcile_jobs=False,
    )
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
        "clarification_pull_dir",
    }
    assert confirmed["fact"]["status"] == "confirmed"
    assert status["project"]["input_mode"] == "step"
    assert (
        next(item for item in status["project"]["facts"] if item["name"] == "material")[
            "value"
        ]
        == "ABS"
    )
    assert {
        item["clarification_id"] for item in status["project"]["open_clarifications"]
    } == {
        "clarification_process",
        "clarification_model_units",
        "clarification_pull_dir",
    }
    assert status["capabilities"]["step"]["status"] == "dependency_missing"
    assert (
        status["ontology"]["snapshot_id"] == dfm.ontology_store.identity().snapshot_id
    )
    assert listed["projects"][0]["project_id"] == created["project_id"]


def test_interactive_dfm_fact_requires_real_response(service, monkeypatch):
    from tools.clarify_tool import clarify_tool
    from tools.dfm_tool import _call

    dfm, temp = service
    created = dfm.project("create", name="Interactive facts")
    project_id = created["project_id"]
    source = temp / "interactive.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: dfm)

    rejected = json.loads(
        _call(
            "project",
            {
                "action": "confirm_fact",
                "project_id": project_id,
                "fact_name": "process",
                "fact_value": "injection",
            },
        )
    )
    assert rejected["ok"] is False
    assert not any(
        item["name"] == "process"
        for item in dfm.project("status", project_id=project_id)["project"]["facts"]
    )

    invalid_answer = json.loads(
        clarify_tool(
            "请确认工艺",
            callback=lambda *_: "unsupported_process",
            dfm_project_id=project_id,
            dfm_fact_name="process",
        )
    )
    assert invalid_answer["ok"] is False
    assert not any(
        item["name"] == "process"
        for item in dfm.project("status", project_id=project_id)["project"]["facts"]
    )

    seen = []

    def answer(question, choices):
        seen.extend(choices)
        assert not any(
            item["name"] == "process"
            for item in dfm.project("status", project_id=project_id)["project"]["facts"]
        )
        return "注塑成型"

    result = json.loads(
        clarify_tool(
            "请确认工艺",
            callback=answer,
            dfm_project_id=project_id,
            dfm_fact_name="process",
        )
    )
    assert seen == ["压铸", "注塑成型"]
    assert result["dfm_fact"]["value"] == "injection"
    assert any(
        item["name"] == "process"
        for item in dfm.project("status", project_id=project_id)["project"]["facts"]
    )

    repeated = json.loads(
        clarify_tool(
            "请确认工艺",
            callback=lambda *_: "injection",
            dfm_project_id=project_id,
            dfm_fact_name="process",
        )
    )
    assert "error" in repeated


def test_dfm_questions_bind_the_active_project_and_startup_factors(
    service, monkeypatch
):
    from tools.clarify_tool import clarify_tool
    from tools.dfm_tool import _call

    dfm, temp = service
    project_id = dfm.project("create", name="Single process question")["project_id"]
    source = temp / "single-question.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: dfm)
    monkeypatch.setattr("tools.dfm_tool.get_dfm_service", lambda: dfm)
    _call(
        "project",
        {"action": "status", "project_id": project_id},
        session_id="dfm-single-question",
    )

    shown = []

    def answer_next(expected_name, answer=None):
        result = json.loads(
            clarify_tool(
                "Unbound Agent question",
                choices=["irrelevant"],
                callback=lambda question, options: (
                    shown.append((question, options))
                    or (answer if answer is not None else options[-1])
                ),
                session_id="dfm-single-question",
            )
        )
        assert result["dfm_fact"]["name"] == expected_name
        return result

    def answer_bound(name, answer):
        result = json.loads(
            clarify_tool(
                f"Confirm {name}",
                choices=["irrelevant"],
                callback=lambda question, options: (
                    shown.append((question, options)) or answer
                ),
                dfm_project_id=project_id,
                dfm_fact_name=name,
                session_id="dfm-single-question",
            )
        )
        assert result["dfm_fact"]["name"] == name
        return result

    process = answer_next("process")
    assert process["dfm_fact"]["value"] == "injection"
    assert process["next_action"] == "clarify"
    assert len(shown[0][1]) == 2
    for name, answer in (("model_units", "mm"), ("pull_dir", "+Z")):
        startup_answer = answer_bound(name, answer)
    assert startup_answer["next_action"] == "discover"
    assert len(shown) == 3
    assert (
        dfm.project("status", project_id=project_id)["project"]["open_clarifications"]
        == []
    )

    discovered = dfm.analysis("discover", project_id=project_id)
    assert discovered["open_clarifications"] == []


def test_requested_process_is_routing_state_until_confirmed_as_a_fact(service):
    dfm, temp = service
    created = dfm.project("create", name="Legacy caller", process="injection")
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)

    added = dfm.project(
        "add_input",
        project_id=created["project_id"],
        path=str(source),
    )
    status = dfm.project("status", project_id=created["project_id"])["project"]

    assert [item["clarification_id"] for item in added["open_clarifications"]] == [
        "clarification_process",
        "clarification_model_units",
        "clarification_pull_dir",
    ]
    assert not any(item["name"] == "process" for item in status["facts"])


def test_discovery_does_not_ask_rule_facts_without_a_worker_feature(service):
    dfm, temp = service
    adapter = dfm.process_registry.get("injection")
    original_requirements = adapter.fact_requirements
    adapter.fact_requirements = lambda: (
        *original_requirements(),
        FactRequirement(
            "screw_boss_only_factor",
            "Boss-only factor?",
            "analysis",
            check_ids=("check.boss.not_discovered",),
        ),
    )
    project_id = dfm.project("create", name="Scoped questions")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))

    blocked = dfm.analysis("discover", project_id=project_id)

    assert blocked["ok"] is True
    assert [item["clarification_id"] for item in blocked["clarifications"]] == [
        "clarification_process",
        "clarification_model_units",
        "clarification_pull_dir",
    ]
    confirmed_process = dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="process",
        fact_value="injection",
    )
    assert confirmed_process["next_action"] == "clarify"
    blocked = dfm.analysis("discover", project_id=project_id)

    assert {item["clarification_id"] for item in blocked["clarifications"]} == {
        "clarification_model_units",
        "clarification_pull_dir",
    }
    for name, value in {"model_units": "mm", "pull_dir": "Z+"}.items():
        confirmed = dfm.project(
            "confirm_fact",
            project_id=project_id,
            fact_name=name,
            fact_value=value,
        )
    assert confirmed["next_action"] == "discover"

    discovered = dfm.analysis("discover", project_id=project_id)
    status = dfm.project("status", project_id=project_id)["project"]

    assert discovered["open_clarifications"] == []
    assert next(
        item["value"] for item in status["facts"] if item["name"] == "pull_dir"
    ) == [0.0, 0.0, 1.0]


def test_published_factor_choices_are_validated_without_fabricated_feature(service):
    dfm, _temp = service
    package_path = (
        Path(__file__).parents[3]
        / "tools/dfm/scopes/injection/ontology_snapshot_v2.json"
    )
    package = json.loads(package_path.read_text(encoding="utf-8"))
    material = next(
        item for item in package["concepts"] if item["concept_id"] == "factor.material"
    )
    material["data_schema"]["enum"] = ["ABS", "PC"]
    package["factor_options"].append({
        "factor_id": "factor.material",
        "option_code": "PC",
        "name_zh": "PC",
        "value": "PC",
        "sort_order": 20,
        "status": "active",
    })
    unhashed = {key: value for key, value in package.items() if key != "content_sha256"}
    package["content_sha256"] = hashlib.sha256(
        json.dumps(
            unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    dfm.ontology_store.install_package(package)

    project_id = dfm.project("create", name="Published factor choices")["project_id"]
    assert dfm.clarification_choices("injection", "material") == ("ABS", "PC")

    with pytest.raises(DFMError) as exc_info:
        dfm.project(
            "confirm_fact",
            project_id=project_id,
            fact_name="material",
            fact_value="glass_fiber",
        )
    assert exc_info.value.code == "fact_invalid"
    assert exc_info.value.details["allowed_values"] == ["ABS", "PC"]

    dfm.project(
        "confirm_fact", project_id=project_id, fact_name="material", fact_value="PC"
    )
    material_facts = [
        item
        for item in dfm.project("status", project_id=project_id)["project"]["facts"]
        if item["name"] == "material" and item["status"] == "confirmed"
    ]
    assert material_facts[-1]["value"] == "PC"


def test_ontology_status_reads_installed_workspace_store(service):
    dfm, _temp = service

    result = dfm.project("ontology_status")

    assert result == {
        "ok": True,
        "source": "installed_workspace",
        "ontology_database": str(dfm.ontology_store.path),
        "ontology": dfm.ontology_store.identity().to_dict(),
    }


def test_sync_ontology_uses_configured_synchronizer_and_returns_installed_identity(
    service,
):
    dfm, _temp = service
    expected_identity = dfm.ontology_store.identity().to_dict()

    class Synchronizer:
        def sync_once(self):
            return {"changed": False, "snapshot_id": expected_identity["snapshot_id"]}

    dfm.ontology_synchronizer = Synchronizer()
    result = dfm.project("sync_ontology")

    assert result == {
        "ok": True,
        "source": "remote_publication",
        "sync": {"changed": False, "snapshot_id": expected_identity["snapshot_id"]},
        "ontology_database": str(dfm.ontology_store.path),
        "ontology": expected_identity,
    }


def test_sync_ontology_requires_configured_endpoint(service):
    dfm, _temp = service

    with pytest.raises(DFMError) as exc_info:
        dfm.project("sync_ontology")

    assert exc_info.value.code == "ontology_sync_unavailable"


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
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="process",
        fact_value="injection",
    )
    dfm.analysis("discover", project_id=project_id)

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


def test_desktop_choice_labels_are_normalized_as_confirmed_facts(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Desktop choices")["project_id"]

    process = dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="process",
        fact_value="注塑成型 (Injection Molding)",
    )
    units = dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="model_units",
        fact_value="毫米 (mm)",
    )

    assert process["fact"]["value"] == "injection"
    assert units["fact"]["value"] == "mm"


def test_legacy_model_length_unit_clarification_is_reconciled(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Legacy unit clarification")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="process",
        fact_value="injection",
    )
    dfm.analysis("discover", project_id=project_id)
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


def test_injection_plan_rejects_non_v2_backend_explicitly(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))

    blocked = dfm.analysis("plan", project_id=project_id)
    assert blocked["ok"] is True
    assert blocked["status"] == "clarification_required"
    assert blocked["next_action"] == "clarify"
    assert [item["clarification_id"] for item in blocked["clarifications"]] == [
        "clarification_process",
        "clarification_model_units",
        "clarification_pull_dir",
    ]
    confirm_step_facts(dfm, project_id)
    awaiting_discovery = dfm.analysis("plan", project_id=project_id)
    assert awaiting_discovery["ok"] is True
    assert awaiting_discovery["status"] == "discovery_required"
    dfm.analysis("discover", project_id=project_id)
    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("plan", project_id=project_id)
    assert exc_info.value.code == "geometry_contract_unsupported"
    assert exc_info.value.details == {
        "analyzer_key": "step",
        "required_contract": "dfm.geometry/v2",
    }


def test_occt_plan_stops_when_no_supported_rule_is_applicable(service, monkeypatch):
    dfm, temp = service
    capability = {
        "contract_version": "dfm.geometry/v2",
        "geometrics": [
            {
                "geometric_id": "G_WALL_THK_MIN",
                "feature_kind": "main_wall",
                "support_status": "supported",
                "scope_modes": ["feature"],
                "direction_modes": ["none"],
                "requires_complete_inventory": False,
                "context_schema": {},
            }
        ],
    }

    class AvailableOcctAnalyzer:
        key = "occt_cpp"
        version = "test"
        supported_inputs = ("step",)

        @staticmethod
        def capability(_context):
            return Capability(
                "occt_cpp", CapabilityStatus.AVAILABLE, "available for test"
            )

        @staticmethod
        def run(_context, _cancellation):
            raise AssertionError("a no-applicable-checks plan must not start")

    project_id = dfm.project("create", name="Unsupported OCCT rule set")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    dfm.registry.register(AvailableOcctAnalyzer())
    dfm.occt_geometry_capability = capability
    dfm.ontology_store.configure_geometric_bindings(
        geometry_binding_index(capability)
    )
    monkeypatch.setattr(
        "tools.dfm.service.compile_occt_injection_plan",
        lambda adapter, context, raw_parameters, capability: replace(
            adapter.compile(context, raw_parameters),
            rules={},
            operations=[],
            rule_bindings=[],
            binding_selectors={},
        ),
    )
    confirm_step_facts(dfm, project_id)
    dfm.analysis("discover", project_id=project_id)

    result = dfm.analysis("plan", project_id=project_id, analyzer_key="occt_cpp")

    assert result["status"] == "no_applicable_checks"
    assert result["next_action"] == "complete"
    assert result["reason_code"] == "no_applicable_supported_rules"
    assert result["supported_geometric_ids"] == sorted(
        geometry_binding_index(dfm.occt_geometry_capability or {})
    )
    assert result["ontology"]["rule_set_version"]
    assert result["published_check_ids"]
    assert "plan" not in result
    manifest = dfm.project("status", project_id=project_id)["project"]
    assert all(item["phase"] == "discovery" for item in manifest["plans"])


def test_geometric_binding_keeps_one_local_scope_waiting_while_another_is_ready(
    service,
):
    dfm, _temp = service
    input_sha256 = "a" * 64
    geometry_ref = GeometryRef("face", 7, input_sha256)
    feature = FeatureRecord(
        "feature-wall",
        "main_wall",
        [],
        1.0,
        input_sha256=input_sha256,
        properties={"geometry_refs": [geometry_ref.to_dict()]},
        status="confirmed",
    )
    scopes = [
        FormingScopeRecord(
            "SIDE_01",
            "snapshot_model_2",
            "local",
            None,
            "model",
            None,
            "candidate",
            [geometry_ref],
        ),
        FormingScopeRecord(
            "SIDE_02",
            "snapshot_model_3",
            "local",
            [0.0, 1.0, 0.0],
            "model",
            "user_confirmed",
            "confirmed",
            [geometry_ref],
        ),
    ]
    review_groups = [
        ReviewGroupRecord(
            f"review.structure.{scope_id}",
            "local_structure",
            [scope_id, feature.feature_id],
            character * 64,
            "user",
            "confirmed",
            "2026-10-21T00:00:00Z",
        )
        for scope_id, character in (("SIDE_01", "b"), ("SIDE_02", "c"))
    ]
    process_plan = ProcessPlan(
        process="injection",
        adapter_version="test",
        scope_id="injection.geometry-core",
        scope_version="test",
        rules={"rule-draft": EffectiveRule(1.0, "deg", "test")},
        operations=[],
        accepted_inputs={"step"},
        rule_bindings=[
            GeometricRuleBinding(
                "binding-draft",
                "G_WALL_IN_DRAFT",
                "rule-draft",
                ">=",
                "identity",
            )
        ],
        binding_selectors={
            "binding-draft": {"actual": {"feature_kind": "main_wall"}}
        },
    )
    dfm.occt_geometry_capability = {
        "contract_version": "dfm.geometry/v2",
        "geometrics": [
            {
                "geometric_id": "G_WALL_IN_DRAFT",
                "feature_kind": "main_wall",
                "support_status": "supported",
                "scope_modes": ["feature"],
                "direction_modes": ["local"],
                "requires_complete_inventory": False,
                "context_schema": {
                    "required": [
                        "forming_scope_id",
                        "draw_direction",
                        "coordinate_system",
                        "direction_source",
                    ]
                },
            }
        ]
    }

    bound = dfm._bind_geometric_scope(
        process_plan,
        SimpleNamespace(
            features=[feature],
            forming_scopes=scopes,
            review_groups=review_groups,
        ),
        SimpleNamespace(
            worker_snapshot_id="snapshot_model_4",
            feature_refs=[feature.feature_id],
        ),
    )

    contexts = {
        item.context["forming_scope_id"]: item.context
        for item in bound.rule_bindings
    }
    assert contexts == {
        "SIDE_01": {"forming_scope_id": "SIDE_01"},
        "SIDE_02": {
            "forming_scope_id": "SIDE_02",
            "draw_direction": [0.0, 1.0, 0.0],
            "coordinate_system": "model",
            "direction_source": "user_confirmed",
        },
    }


def test_input_or_confirmed_fact_invalidates_prior_plan(service):
    dfm, temp = service
    project_id = dfm.project("create", name="Bracket")["project_id"]
    source = temp / "part.step"
    source.write_bytes(STEP_PAYLOAD)
    dfm.project("add_input", project_id=project_id, path=str(source))
    confirm_step_facts(dfm, project_id)
    fragment = PlanFragmentRecord(
        "fragment-ready",
        "snapshot-worker",
        "snapshot-discovery",
        "dfm.geometry/v2:test",
        "a" * 64,
        [],
        [],
        [GeometricRequest("feature-main-wall", ["G_WALL_THK_MIN"], {})],
    )
    plan = PlanRecord(
        "plan-ready",
        "step",
        ["occt_cpp"],
        "ready",
        "2026-10-21T00:00:00Z",
        process="injection",
        plan_fragments=[fragment],
    )
    dfm._store(project_id).update(
        lambda current: replace(current, plans=[*current.plans, plan])
    )

    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="model_units",
        fact_value="inch",
    )

    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id, plan_id=plan.plan_id)
    assert exc_info.value.code == "plan_not_ready"
    assert exc_info.value.details["status"] == "invalidated"


def test_start_requires_explicit_plan_id(service):
    dfm, _temp = service
    project_id = dfm.project("create", name="Missing plan id")["project_id"]

    with pytest.raises(DFMError) as exc_info:
        dfm.analysis("start", project_id=project_id)

    assert exc_info.value.code == "plan_id_required"
    assert exc_info.value.details == {"action": "start"}


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
        item["fact_id"] for item in facts if item["name"] in {"process", "model_units"}
    }


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

    blocked = dfm.analysis("discover", project_id=project_id)

    assert [item["clarification_id"] for item in blocked["clarifications"]] == [
        "clarification_process",
        "clarification_model_units",
        "clarification_pull_dir",
    ]
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="process",
        fact_value="压铸",
    )
    blocked = dfm.analysis("discover", project_id=project_id)

    assert [item["clarification_id"] for item in blocked["clarifications"]] == [
        "clarification_model_units",
        "clarification_pull_dir",
    ]
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="model_units",
        fact_value="mm",
    )
    dfm.project(
        "confirm_fact",
        project_id=project_id,
        fact_name="pull_dir",
        fact_value="+Z",
    )
    discovery = dfm.analysis("discover", project_id=project_id)
    result = dfm.analysis("plan", project_id=project_id)
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
    assert status["project"]["process_source"] == "user_confirmed"


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
