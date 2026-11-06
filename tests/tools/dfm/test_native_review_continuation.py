"""Actual Worker and journal contracts for confirmation and Agent continuation."""
from dataclasses import replace
import json
import os

import pytest

from tests.tools.dfm.test_native_geometry_edits import model
from tools.clarify_tool import clarify_tool
from tools.dfm.errors import DFMError, ManifestCommitError
from tools.dfm.project import journal
from tools.dfm.service import DFMService
from tools.tool_result_storage import maybe_persist_tool_result

pytestmark = pytest.mark.skipif(not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"), reason="native Worker not configured")


@pytest.mark.parametrize("legacy", [False, True])
def test_restart_retains_questions_and_current_viewer_without_warm_binding_cache(model, monkeypatch, legacy):
    service, identity, _, _ = model
    service.analysis("confirm_discovery", project_id=identity)
    expected = service.review_state(identity)
    assert expected["open_clarification_count"] > 0
    assert expected["viewer_manifest"]
    store = service._store(identity)
    if legacy:
        def without_evidence(current):
            discovery = dict(current.capabilities["geometry_discovery"])
            discovery.pop("geometry_capability")
            return replace(current, capabilities={**current.capabilities, "geometry_discovery": discovery})
        store.update(without_evidence)
    before = store.load()
    fresh = DFMService(config=service.config, workspace=service.workspace, reconcile_jobs=False)
    try:
        assert fresh.occt_geometry_capability is None
        if not legacy:
            monkeypatch.setattr(fresh.geometry_sessions, "acquire", lambda *_a, **_k: pytest.fail("State reads must not start a Worker"))
        state = fresh.review_state(identity)
        assert state["open_clarification_count"] == expected["open_clarification_count"]
        assert state["next_call"] == expected["next_call"]
        assert state["viewer_manifest"] == expected["viewer_manifest"]
        assert store.load() == before
        if not legacy:
            assert not fresh.ontology_store.geometric_scope_specs("injection")
    finally:
        fresh.close()


def test_first_plan_after_restart_preserves_ready_fragment_continuation(model, monkeypatch):
    from tools import dfm_tool
    service, identity, _, _ = model
    service.analysis("confirm_discovery", project_id=identity)
    fresh = DFMService(config=service.config, workspace=service.workspace, reconcile_jobs=False)
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: fresh)
    try:
        result = json.loads(dfm_tool._call("analysis", {"action": "plan", "project_id": identity}))
        # This fixture's rules permit a ready thickness fragment alongside
        # unresolved questions; pending inputs must not suppress ready work.
        assert result["plan"]["status"] == "ready"
        assert result["plan"]["plan_fragment_count"] > 0
        assert result["next_call"] == {"tool": "dfm_analysis", "arguments": {
            "action": "start", "project_id": identity, "plan_id": result["plan"]["plan_id"]}}
        assert fresh.review_state(identity)["open_clarification_count"] > 0
        assert result["continuation_required"]
        assert len(json.dumps(result)) < 6500
    finally:
        fresh.close()


def test_confirmation_reply_and_agent_query_share_current_state(model, monkeypatch):
    from tools import dfm_tool
    service, identity, _, _ = model
    monkeypatch.setattr("tools.dfm.service.get_dfm_service", lambda: service)
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: service)
    raw = clarify_tool("", dfm_project_id=identity, dfm_discovery_review=True,
                       callback=lambda _question, choices: choices[0])
    assert maybe_persist_tool_result(raw, "clarify", "confirmation", env=None) == raw
    response = json.loads(raw)
    query = json.loads(dfm_tool._call("analysis", {"action": "review_context", "project_id": identity}))
    assert response["dfm_discovery_review"] == query
    assert query["review_group_counts"]["ordinary_structure"].get("pending_review", 0) == 0
    assert query["main_forming"][0]["status"] == "confirmed"
    assert query["next_call"]["tool"] == "clarify"
    assert query["pending_review_group_count"] > 0  # incomplete local proposal remains pending
    before = service._store(identity).load()
    main = query["main_forming"][0]["review_group_id"]
    repeated = json.loads(dfm_tool._call("analysis", {"action": "confirm_discovery", "project_id": identity,
        "review_group_ids": [main], "expected_revision": query["revision"], "expected_snapshot_id": query["snapshot_id"]}))
    assert repeated["operation"]["unchanged"]
    after = service._store(identity).load()
    assert after.revision == before.revision
    assert "review_save_failure" not in after.capabilities
    assert after.geometry_edits == before.geometry_edits


def test_conflicts_and_invalid_input_do_not_create_save_barriers(model):
    service, identity, _, _ = model
    before = service._store(identity).load()
    for params in ({"review_group_ids": ["missing"]}, {"expected_revision": before.revision - 1}):
        with pytest.raises(DFMError):
            service.analysis("confirm_discovery", project_id=identity, **params)
        current = service._store(identity).load()
        assert current.revision == before.revision
        assert "review_save_failure" not in current.capabilities


def test_actual_commit_failure_blocks_until_same_save_succeeds(model, monkeypatch):
    service, identity, _, _ = model
    append = journal.append
    failed = False
    def fail_once(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected durable write failure")
        return append(*args)
    monkeypatch.setattr(journal, "append", fail_once)
    with pytest.raises(ManifestCommitError):
        service.analysis("confirm_discovery", project_id=identity)
    store = service._store(identity)
    assert store.load().capabilities["review_save_failure"]["failure_category"] == "save_failed"
    with pytest.raises(DFMError) as blocked:
        service.analysis("plan", project_id=identity)
    assert blocked.value.code == "review_save_failed"
    service.analysis("confirm_discovery", project_id=identity)
    assert "review_save_failure" not in store.load().capabilities


def test_post_commit_viewer_failure_does_not_turn_saved_review_into_failed_save(model, monkeypatch):
    service, identity, _, _ = model
    def fail_viewer(*args, **kwargs):
        raise OSError("injected derived viewer failure")
    monkeypatch.setattr("tools.dfm.service.materialize_discovery_viewer_manifest", fail_viewer)
    with pytest.raises(OSError):
        service.analysis("confirm_discovery", project_id=identity)
    current = service._store(identity).load()
    assert all(row.status == "confirmed" for row in current.review_groups if row.kind == "ordinary_structure")
    assert "review_save_failure" not in current.capabilities
