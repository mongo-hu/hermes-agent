"""Committed-state projection stays usable without a terminal or sandbox."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tools.dfm.contracts import DiscoverySnapshotRecord, FeatureRecord, ReviewGroupRecord, ClarificationRecord
from tools.dfm.errors import DFMError
from tools.dfm.project.manifest import ManifestStore
from tools.dfm.project.review_barrier import reconcile_legacy_confirmation_failure
from tools.dfm.project.workspace import DFMWorkspace
from tools.dfm.review_state import REVIEW_STATE_MAX_CHARS
from tools.dfm.service import DFMService
from tools.tool_result_storage import maybe_persist_tool_result


@pytest.fixture
def large_review(tmp_path):
    workspace = DFMWorkspace(tmp_path / "dfm")
    manifest = workspace.create_project("large review projection")
    store = ManifestStore(workspace.project_dir(manifest.project_id))
    # Payload volume reproduces the failure independently of the CAD algorithm.
    features = [FeatureRecord(f"feature-{index}", "rib", [], 1.0,
        properties={"geometry_refs": [{"kind": "face", "index": face} for face in range(1, 160)]})
        for index in range(485)]
    snapshot = DiscoverySnapshotRecord("snapshot-current", "now", {}, [],
        [row.feature_id for row in features], [], [], {}, "a" * 64)
    from tools.dfm.review import structure_review_groups
    review_groups = structure_review_groups(features, [], snapshot_id=snapshot.snapshot_id, created_at="now")
    main = ReviewGroupRecord("review-main", "product_forming", ["MAIN"], "b" * 64, "user", "confirmed", "now", "now")
    manifest = store.update(lambda current: replace(current, features=features,
        discovery_snapshots=[snapshot], review_groups=[*review_groups, main]))
    service = object.__new__(DFMService)
    service.discovery = SimpleNamespace(current_snapshot=lambda current: current.discovery_snapshots[-1])
    service._structure_review_groups = lambda current, snapshot: current.review_groups
    service._store = lambda identity: store
    service._open_clarifications = lambda current, **kw: current.clarifications
    return service, store, manifest


def test_tool_query_reads_journal_and_pages_complete_ids_without_raw_geometry(large_review, monkeypatch):
    from tools import dfm_tool
    service, store, manifest = large_review
    monkeypatch.setattr(dfm_tool, "get_dfm_service", lambda: service)
    checkpoint = json.loads(store.path.read_text(encoding="utf-8"))
    assert checkpoint["revision"] < manifest.revision
    seen = set()
    args = {"action": "review_context", "project_id": manifest.project_id}
    while True:
        raw = dfm_tool._call("analysis", args)
        assert len(raw) <= REVIEW_STATE_MAX_CHARS
        assert maybe_persist_tool_result(raw, "dfm_analysis", "test", env=None) == raw
        state = json.loads(raw)
        assert state["revision"] == manifest.revision
        assert state["feature_count"] == len(manifest.features)
        assert state["main_forming"][0]["status"] == "confirmed"
        assert "geometry_refs" not in raw
        ids = {row["review_group_id"] for row in state["review_groups"]}
        assert not (ids & seen)
        seen |= ids
        cursor = state["review_page"]["next_cursor"]
        if not cursor:
            break
        args["review_cursor"] = cursor
    assert seen == {row.review_group_id for row in manifest.review_groups if row.status == "pending_review"}
    selected = json.loads(dfm_tool._call("analysis", {"action": "review_context",
        "project_id": manifest.project_id, "review_group_ids": ["review-main"]}))
    assert selected["review_groups"][0]["status"] == "confirmed"


def test_page_cursor_rejects_a_new_revision(large_review):
    service, store, manifest = large_review
    page = service.review_state(manifest.project_id)
    store.update(lambda current: replace(current, name="another revision"))
    with pytest.raises(DFMError) as failure:
        service.review_state(manifest.project_id, cursor=page["review_page"]["next_cursor"])
    assert failure.value.code == "review_cursor_stale"
    assert "review_save_failure" not in store.load().capabilities


def test_confirmed_structures_continue_to_questions_while_local_review_remains(large_review):
    service, store, manifest = large_review
    local = ReviewGroupRecord("review-local", "forming_scope", ["SIDE"], "c" * 64, "user", "pending_review", "now")
    question = ClarificationRecord("clarification_material", "Choose material", "open")
    current = store.update(lambda current: replace(current,
        review_groups=[*[replace(row, status="confirmed") for row in current.review_groups], local],
        clarifications=[question]))
    result = service.review_state(current.project_id)
    assert result["next_call"] == {"tool": "clarify", "arguments": {
        "dfm_project_id": current.project_id, "dfm_fact_name": "material"}}
    assert result["pending_review_group_count"] == 1


@pytest.mark.parametrize("category,pending_edit,recover", [(None, False, True), ("save_failed", False, False), (None, True, False)])
def test_legacy_failure_recovery_requires_proof_of_committed_confirmation(large_review, category, pending_edit, recover):
    _, store, manifest = large_review
    failure = {"identity": ["confirm_discovery", ["product_forming:MAIN"]],
               "reason": "review_group_stale", "message": "already closed"}
    if category:
        failure["failure_category"] = category
    store.update(lambda current: replace(current,
        capabilities={**current.capabilities, "review_save_failure": failure},
        geometry_edits=[{"edit_id": "incomplete", "status": "pending"}] if pending_edit else []))
    state = reconcile_legacy_confirmation_failure(store)
    assert ("review_save_failure" not in state.capabilities) == recover
    if recover:
        assert state.capabilities["review_save_recovery"]["previous_failure"] == failure
    assert state.review_groups == manifest.review_groups
