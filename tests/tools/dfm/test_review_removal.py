"""Removal is a durable review decision, not an empty face edit or hidden card."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.dfm.config import DFMConfig
from tools.dfm.contracts import (
    ArtifactRecord, FeatureRecord, FormingScopeRecord, GeometricRequest, GeometryRef,
    InputRecord, PlanFragmentRecord, PlanRecord, ReviewGroupRecord,
)
from tools.dfm.discovery import DiscoveryEngine
from tools.dfm.errors import DFMError
from tools.dfm.feature_exclusions import active_features, retire_excluded_fragments
from tools.dfm.feature_recognition.base import FeatureRecognitionResult
from tools.dfm.project.geometry_edits import GeometryEdit
from tools.dfm.service import DFMService


@pytest.fixture
def project(tmp_path):
    service = DFMService(config=DFMConfig(geometry_backend="pythonocc_internal",
        geometry_executable=str(tmp_path / "missing-worker.exe")), reconcile_jobs=False)
    try:
        project_id = service.project("create", name="review removal")["project_id"]
        store = service._store(project_id)
        digest = "a" * 64
        refs = [GeometryRef("face", index, digest, "topology", f"face-{index}") for index in (1, 2)]
        features = [FeatureRecord(identity, kind, ["test"], 0.9, digest,
            properties={"geometry_refs": [ref.to_dict()]}, recognizer="test", recognizer_version="1")
            for identity, kind, ref in zip(("WALL", "BOSS"), ("main_wall", "hollow_boss"), refs)]
        scope = FormingScopeRecord("SIDE", "worker1", "local", [1, 0, 0], "model",
            "user_confirmed", "confirmed", geometry_refs=[refs[1]], kind="undercut",
            mechanism_kind="slider", undercut_geometry_refs=[refs[1]])
        relation = {"relation_id": "membership", "type": "contained_in_forming_scope",
                    "from_ref": "BOSS", "to_ref": "SIDE", "geometry_refs": [refs[1].to_dict()]}
        artifacts = []
        for kind in ("render_scene", "topology_map"):
            relative_path = f"artifacts/{kind}.json"
            path = store.project_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            artifacts.append(ArtifactRecord(kind, kind, relative_path, "application/json", "now"))
        state = store.update(lambda current: replace(current,
            inputs=[InputRecord("input", "step", "model.step", "inputs/model.step", 1, digest, "now")],
            features=features, forming_scopes=[scope], artifacts=artifacts,
            capabilities={"geometry_discovery": {"inputs": {digest: {
                "worker_snapshot_id": "worker1", "forming_scopes": [scope.to_dict()],
                "relationships": [relation], "annotations": [], "coverage": [],
                "artifact_refs": [artifact.artifact_id for artifact in artifacts],
            }}}}))
        frozen, snapshot = service.discovery.freeze(state)
        groups = service._structure_review_groups(frozen, snapshot, confirmed_scope_ids={"SIDE"})
        store.update(lambda current: replace(frozen, revision=current.revision, review_groups=groups))
        yield service, project_id, store
    finally:
        service.close()


def remove_boss(service, project_id):
    context = service.analysis("review_context", project_id=project_id)
    group = next(row for row in context["review_groups"]
                 if row["kind"] == "ordinary_structure" and row["target_refs"] == ["BOSS"])
    params = dict(project_id=project_id, review_group_id=group["review_group_id"],
        expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    return service.analysis("remove_review_group", **params), params


def test_removal_persists_across_process_and_preserves_independent_forming_review(project):
    service, project_id, store = project
    before = store.load()
    forming = next(group for group in before.review_groups if group.kind == "forming_scope")
    response, params = remove_boss(service, project_id)
    assert {feature["feature_id"] for feature in response["features"]} == {"WALL"}
    after = store.load()
    assert next(feature.status for feature in after.features if feature.feature_id == "BOSS") == "rejected"
    assert after.geometry_edits == before.geometry_edits
    assert after.forming_scopes == before.forming_scopes
    assert after.capabilities["geometry_discovery"] == before.capabilities["geometry_discovery"]
    assert next(group for group in after.review_groups if group.kind == "forming_scope") == forming
    assert next(group for group in response["review_groups"] if group["kind"] == "forming_scope") == forming.to_dict()
    assert not any(group["kind"] in {"undercut_scope", "local_structure", "local_direction"}
                   for group in response["review_groups"])
    assert not response["relationships"]
    viewer = json.loads(Path(response["viewer_manifest"]).read_text(encoding="utf-8"))
    assert {feature["feature_id"] for feature in viewer["features"]} == {"WALL"}
    assert not viewer["relationships"]
    assert all("BOSS" not in group["target_refs"] for group in viewer["review_groups"])
    script = "from pathlib import Path; from tools.dfm.project.manifest import ManifestStore; import json,sys; print(json.dumps(ManifestStore(Path(sys.argv[1])).load().to_dict()))"
    loaded = json.loads(subprocess.check_output([sys.executable, "-c", script, str(store.project_dir)], text=True))
    record = loaded["capabilities"]["feature_exclusions"]["a" * 64]["BOSS"]
    assert record["feature"]["feature_id"] == "BOSS"
    assert record["snapshot_id"] == params["expected_snapshot_id"]
    with pytest.raises(DFMError) as conflict:
        service.analysis("remove_review_group", **params)
    assert conflict.value.code == "manifest_conflict"
    params.update(expected_revision=response["revision"], expected_snapshot_id=response["snapshot_id"])
    with pytest.raises(DFMError) as conflict:
        service.analysis("remove_review_group", **params)
    assert conflict.value.code == "review_group_stale"


def test_full_rediscovery_and_worker_projection_cannot_revive_exclusions(project, tmp_path):
    service, project_id, store = project
    original = store.load()
    remove_boss(service, project_id)
    state = store.load()
    provider = SimpleNamespace(key="test", version="1",
        capability=lambda *args: {"status": "available"},
        recognize=Mock(return_value=FeatureRecognitionResult(features=original.features, diagnostics={"coverage": []})))
    engine = DiscoveryEngine(geometry_provider=provider)
    # The rejected feature may disappear for a recognition round, then reappear.
    provider.recognize.return_value = FeatureRecognitionResult(features=[], diagnostics={"coverage": []})
    absent = engine.refresh_candidates(state, project_dir=tmp_path, force=True)
    assert absent.features == []
    provider.recognize.return_value = FeatureRecognitionResult(features=original.features, diagnostics={"coverage": []})
    regenerated = engine.refresh_candidates(absent, project_dir=tmp_path, force=True)
    assert next(feature.status for feature in regenerated.features if feature.feature_id == "BOSS") == "rejected"
    _, snapshot = engine.freeze(regenerated)
    assert snapshot.feature_refs == ["WALL"]
    # A committed Worker response may contain the complete, unchanged raw graph.
    raw = original.capabilities["geometry_discovery"]["inputs"]["a" * 64]
    projected = replace(original.features[1], properties={"geometry_refs": [{"kind": "face", "index": 3}]})
    service.discovery.geometry_provider.project_snapshot = Mock(return_value=FeatureRecognitionResult(
        features=[original.features[0], projected], diagnostics={}))
    committed, frozen = service._persist_forming_snapshot(project_id, "a" * 64,
        {**raw, "snapshot_id": "worker2", "features": []}, reviewed_scope_ids=set(), confirmed=False)
    assert next(feature.status for feature in committed.features if feature.feature_id == "BOSS") == "rejected"
    assert "BOSS" not in frozen.feature_refs
    different_input = replace(projected, input_sha256="c" * 64)
    assert active_features(replace(committed, features=[different_input])) == [different_input]


@pytest.mark.parametrize("kind", ["product_forming", "undercut_scope", "local_structure", "local_direction"])
def test_noncurrent_review_groups_cannot_be_removed(project, kind):
    service, project_id, store = project
    store.update(lambda current: replace(current, review_groups=[*current.review_groups,
        ReviewGroupRecord("forbidden", kind, ["SIDE"], "c" * 64, "user", "pending_review", "now")]))
    context = service.analysis("review_context", project_id=project_id)
    before = store.load()
    with pytest.raises(DFMError) as failure:
        service.analysis("remove_review_group", project_id=project_id, review_group_id="forbidden",
            expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    assert failure.value.code == "review_group_stale"
    assert store.load() == before


def test_feature_removal_conflicts_with_inflight_geometry_and_old_confirmation(project):
    service, project_id, store = project
    before = store.load()
    edit = GeometryEdit.begin(store, expected_revision=before.revision, input_sha256="a" * 64,
        method="features.revise", params={"snapshot_id": "worker1", "revisions": []})
    response, params = remove_boss(service, project_id)
    transform = Mock(side_effect=AssertionError("must not resurrect removed feature"))
    with pytest.raises(DFMError) as conflict:
        edit.commit("orphan", transform)
    assert conflict.value.code == "geometry_edit_conflict"
    transform.assert_not_called()
    with pytest.raises(DFMError) as conflict:
        service.analysis("confirm_discovery", project_id=project_id,
            review_group_ids=[params["review_group_id"]], expected_revision=params["expected_revision"],
            expected_snapshot_id=params["expected_snapshot_id"])
    assert conflict.value.code == "geometry_edit_conflict"
    assert {row["feature_id"] for row in response["features"]} == {"WALL"}


def test_worker_removal_commit_refuses_a_concurrently_confirmed_review(project):
    service, project_id, store = project
    before = store.load()
    group = next(row for row in before.review_groups if row.kind == "ordinary_structure")
    edit = GeometryEdit.begin(store, expected_revision=before.revision, input_sha256="a" * 64,
        method="annotations.revise", params={"snapshot_id": "worker1", "annotations": []})
    store.update(lambda current: replace(current, review_groups=[replace(row, status="confirmed")
        if row.review_group_id == group.review_group_id else row for row in current.review_groups]))
    current = store.load()
    raw = current.capabilities["geometry_discovery"]["inputs"]["a" * 64]
    with pytest.raises(DFMError) as conflict:
        service._persist_forming_snapshot(project_id, "a" * 64, {**raw, "snapshot_id": "orphan"},
            reviewed_scope_ids=set(), confirmed=False, edit=edit, pending_removal_group=group)
    assert conflict.value.code == "review_group_stale"
    assert store.load() == current


def fragment(identity, feature_id, dependencies=None):
    return PlanFragmentRecord(identity, "worker1", "discovery1", "v2", "a" * 64,
        [], [], [GeometricRequest(feature_id, ["G_WALL_THK_MIN"])],
        status="pending_review", reference_dependencies=dependencies or [])


def test_removal_invalidates_primary_and_reference_plans_without_rewriting_fragments(project):
    service, project_id, store = project
    dependency = {"feature_id": "WALL", "geometric_id": "G_WALL_THK_MIN", "relation_id": "ref",
        "type": "reference", "target_feature_id": "BOSS", "target_feature_kind": "hollow_boss",
        "geometry_refs": [{"kind": "face", "index": 2}], "review_group_refs": []}
    removed, reference, unaffected = fragment("removed", "BOSS"), fragment("reference", "WALL", [dependency]), fragment("unaffected", "WALL")
    plan = PlanRecord("plan1", "step", ["occt_cpp"], "waiting_input", "now", phase="analysis",
        plan_fragments=[removed, reference, unaffected])
    store.update(lambda current: replace(current, plans=[plan]))
    response, _ = remove_boss(service, project_id)
    assert store.load().plans[0].status == "invalidated"
    assert store.load().plans[0].plan_fragments == plan.plan_fragments
    replacements = retire_excluded_fragments(plan.active_plan_fragments(), [], {"BOSS"}, response["snapshot_id"])
    assert {row.supersedes for row in replacements} == {"removed", "reference"}
    updated = replace(plan, plan_fragments=[*plan.plan_fragments, *replacements])
    assert [row.fragment_id for row in updated.active_plan_fragments()] == ["unaffected"]
    assert not retire_excluded_fragments(plan.active_plan_fragments(), replacements, {"BOSS"}, response["snapshot_id"])
