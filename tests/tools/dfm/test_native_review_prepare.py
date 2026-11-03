"""Review survives a lost session head and stages directions without confirmation."""

import json
import os
from pathlib import Path

import pytest

from tools.dfm.config import DFMConfig
from tools.dfm.errors import DFMError
from tools.dfm.service import DFMService

pytestmark = pytest.mark.skipif(not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"), reason="native Worker not configured")


@pytest.fixture
def project():
    service = DFMService(config=DFMConfig(geometry_backend="occt_cpp_external",
        geometry_executable=os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"], geometry_timeout_seconds=120,
        geometry_job_poll_interval_ms=20), reconcile_jobs=False)
    try:
        identity = service.project("create", name="review recovery")["project_id"]
        source = Path(__file__).resolve().parents[2] / "fixtures/dfm/step/injection_plate_with_hole.step"
        service.project("add_input", project_id=identity, path=str(source))
        for name, value in {"process": "injection", "model_units": "mm", "pull_dir": [0, 0, 1]}.items():
            service.project("confirm_fact", project_id=identity, fact_name=name, fact_value=value)
        service.analysis("discover", project_id=identity)
        yield service, identity
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["missing_head", "changed_build"])
def test_prepare_regenerates_a_missing_review_head_and_archives_recovery(project, failure):
    service, identity = project
    old = service.analysis("review_context", project_id=identity)
    before = {row["feature_id"]: row["properties"]["geometry_refs"] for row in old["features"]}
    recovery = next(service.workspace.project_dir(identity).glob("artifacts/geometry_sessions/*/session-recovery.json"))
    state = json.loads(recovery.read_text(encoding="utf-8"))
    if failure == "missing_head":
        state["steps"] = []
        state["current_snapshot_id"] = state["opened_snapshot_id"]
    else:
        state["dependency_fingerprint"] = "0" * 64
    recovery.write_text(json.dumps(state), encoding="utf-8")
    service.geometry_sessions.close_project(identity)
    service.geometry_sessions._recovery.pop(identity, None)  # Hermes restart also loses its in-memory replay cache.
    prepared = service.analysis("prepare_review", project_id=identity, expected_revision=old["revision"], expected_snapshot_id=old["snapshot_id"])
    assert prepared["session_restored"] is True
    assert prepared["snapshot_id"] != old["snapshot_id"]
    assert {row["feature_id"]: row["properties"]["geometry_refs"] for row in prepared["features"]} == before
    assert list(recovery.parent.glob("session-recovery.archived-*.json"))
    assert Path(prepared["viewer_manifest"]).is_file()
    again = service.analysis("prepare_review", project_id=identity, expected_revision=prepared["revision"], expected_snapshot_id=prepared["snapshot_id"])
    assert not again["session_restored"]
    assert again["snapshot_id"] == prepared["snapshot_id"]


def test_one_forming_plan_is_staged_and_confirmed_without_structural_gates(project, monkeypatch):
    service, identity = project
    context = service.analysis("review_context", project_id=identity)
    refs = context["features"][0]["properties"]["geometry_refs"]
    service.analysis("restructure_forming_scopes", project_id=identity, expected_revision=context["revision"],
        expected_snapshot_id=context["snapshot_id"], forming_scopes=[{
            "forming_scope_id": "SIDE", "kind": "undercut", "mode": "local",
            "geometry_refs": refs, "undercut_geometry_refs": refs[:1]}])
    context = service.analysis("review_context", project_id=identity)
    scope = next(row for row in context["forming_scopes"] if row["forming_scope_id"] == "SIDE")
    staged = service.analysis("stage_forming_direction", project_id=identity, forming_scope={**scope, "draw_direction": [1, 0, 0]},
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    assert staged["status"] == "forming_direction_staged"
    context = service.analysis("review_context", project_id=identity)
    groups = [row for row in context["review_groups"] if "SIDE" in row["target_refs"]]
    assert len(groups) == 1 and groups[0]["kind"] == "forming_scope"
    assert groups[0]["status"] == "pending_review"
    with pytest.raises(DFMError) as missing:
        service.analysis("confirm_discovery", project_id=identity, review_group_ids=[groups[0]["review_group_id"]],
            expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    assert missing.value.code == "forming_scope_invalid"
    scope = next(row for row in context["forming_scopes"] if row["forming_scope_id"] == "SIDE")
    assert scope["status"] == "candidate" and scope["draw_direction"] == [1, 0, 0]
    service.analysis("stage_forming_direction", project_id=identity,
        forming_scope={**scope, "mechanism_kind": "slider"},
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    context = service.analysis("review_context", project_id=identity)
    group = next(row for row in context["review_groups"] if row["kind"] == "forming_scope")
    session = service.geometry_sessions.acquire(identity).session
    original_call = session.call
    methods = []
    def tracked(method, params, **kwargs):
        methods.append(method)
        return original_call(method, params, **kwargs)
    monkeypatch.setattr(session, "call", tracked)
    service.analysis("confirm_discovery", project_id=identity, review_group_ids=[group["review_group_id"]],
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    final = service.analysis("review_context", project_id=identity)
    plan = next(row for row in final["forming_scopes"] if row["forming_scope_id"] == "SIDE")
    assert plan["status"] == "confirmed"
    assert plan["mechanism_kind"] == "slider" and plan["direction_source"] == "user_confirmed"
    assert plan["undercut_geometry_refs"] == refs[:1] and plan["geometry_refs"] == refs
    assert any(row["kind"] == "ordinary_structure" and row["status"] == "pending_review" for row in final["review_groups"])
    assert "features.discover" not in methods


def test_unchanged_confirmed_plan_keeps_its_snapshot_and_approval(project, monkeypatch):
    service, identity = project
    context = service.analysis("review_context", project_id=identity)
    refs = context["features"][0]["properties"]["geometry_refs"]
    service.analysis("restructure_forming_scopes", project_id=identity,
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"],
        forming_scopes=[{"forming_scope_id": "SIDE", "kind": "undercut", "mode": "local",
                         "geometry_refs": refs, "mechanism_kind": "slider"}])
    context = service.analysis("review_context", project_id=identity)
    service.analysis("revise_forming_scopes", project_id=identity,
        forming_scopes=[{"forming_scope_id": "SIDE", "draw_direction": [2, 0, 0]}],
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    context = service.analysis("review_context", project_id=identity)
    plan = next(row for row in context["forming_scopes"] if row["forming_scope_id"] == "SIDE")
    monkeypatch.setattr(service, "_prepare_geometry_edit", lambda *args, **kwargs: pytest.fail("no-op must not revise Worker"))
    assert plan["draw_direction"] == [1, 0, 0]
    result = service.analysis("stage_forming_direction", project_id=identity, forming_scope={**plan, "draw_direction": [3, 0, 0]},
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    assert result["status"] == "forming_direction_unchanged"
    final = service.analysis("review_context", project_id=identity)
    assert final["snapshot_id"] == context["snapshot_id"]
    assert any(row["kind"] == "forming_scope" and row["status"] == "confirmed" for row in final["review_groups"])


def test_unknown_direction_and_unresolved_forming_faces_can_be_saved_as_a_draft(project):
    service, identity = project
    context = service.analysis("review_context", project_id=identity)
    refs = context["features"][0]["properties"]["geometry_refs"]
    service.analysis("restructure_forming_scopes", project_id=identity,
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"],
        forming_scopes=[{"forming_scope_id": "SIDE", "kind": "undercut", "mode": "local",
                         "geometry_refs": [], "undercut_geometry_refs": refs[:1]}])
    context = service.analysis("review_context", project_id=identity)
    scope = next(row for row in context["forming_scopes"] if row["forming_scope_id"] == "SIDE")
    service.analysis("stage_forming_direction", project_id=identity,
        forming_scope={**scope, "mechanism_kind": "lifter", "draw_direction": None},
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    final = service.analysis("review_context", project_id=identity)
    plan = next(row for row in final["forming_scopes"] if row["forming_scope_id"] == "SIDE")
    assert plan["mechanism_kind"] == "lifter" and plan["draw_direction"] is None
    assert plan["direction_source"] is None and plan["status"] == "candidate"
    assert plan["geometry_refs"] == [] and plan["undercut_geometry_refs"] == refs[:1]
    group = next(row for row in final["review_groups"] if row["kind"] == "forming_scope")
    with pytest.raises(DFMError) as missing:
        service.analysis("confirm_discovery", project_id=identity, review_group_ids=[group["review_group_id"]],
            expected_revision=final["revision"], expected_snapshot_id=final["snapshot_id"])
    assert missing.value.code == "direction_invalid"


def test_missing_replay_does_not_overwrite_manual_edits(project):
    service, identity = project
    context = service.analysis("review_context", project_id=identity)
    feature = context["features"][0]
    service.analysis("update_feature_geometry", project_id=identity, feature_id=feature["feature_id"],
        geometry_refs=feature["properties"]["geometry_refs"][:1], expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    context = service.analysis("review_context", project_id=identity)
    recovery = next(service.workspace.project_dir(identity).glob("artifacts/geometry_sessions/*/session-recovery.json"))
    original = recovery.read_bytes()
    recovery.unlink()
    service.geometry_sessions.close_project(identity)
    service.geometry_sessions._recovery.pop(identity, None)
    with pytest.raises(DFMError) as error:
        service.analysis("prepare_review", project_id=identity, expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    assert error.value.code == "geometry_recovery_review_required"
    assert not recovery.exists()
    assert service.analysis("review_context", project_id=identity)["snapshot_id"] == context["snapshot_id"]
    recovery.write_bytes(original)


def test_edit_preparation_refreshes_an_old_review_but_mutations_keep_snapshot_guards(project):
    service, identity = project
    original = service.analysis("review_context", project_id=identity)
    feature = original["features"][0]
    service.analysis("restructure_forming_scopes", project_id=identity, expected_revision=original["revision"],
        expected_snapshot_id=original["snapshot_id"], forming_scopes=[{
            "forming_scope_id": "SIDE_LATER", "kind": "undercut", "mode": "local",
            "geometry_refs": feature["properties"]["geometry_refs"]}])
    latest = service.analysis("review_context", project_id=identity)
    assert latest["snapshot_id"] != original["snapshot_id"]
    refreshed = service.analysis("prepare_review", project_id=identity,
        expected_revision=original["revision"], expected_snapshot_id=original["snapshot_id"])
    assert refreshed["review_refreshed"]
    assert refreshed["snapshot_id"] == latest["snapshot_id"]
    with pytest.raises(DFMError) as error:
        service.analysis("update_feature_geometry", project_id=identity, feature_id=feature["feature_id"],
            geometry_refs=feature["properties"]["geometry_refs"][:1], expected_revision=original["revision"],
            expected_snapshot_id=original["snapshot_id"])
    assert error.value.code == "geometry_edit_conflict"
    assert error.value.details["current_snapshot_id"] == latest["snapshot_id"]
