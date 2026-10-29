"""Real OCCT snapshots, user range edits and durable cross-side commits."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Event

import pytest

from tools.dfm.config import DFMConfig
from tools.dfm.errors import DFMError
from tools.dfm.project.geometry_edits import GeometryEdit
from tools.dfm.service import DFMService


pytestmark = pytest.mark.skipif(
    not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"),
    reason="native Worker not configured",
)


@pytest.fixture
def model(tmp_path):
    service = DFMService(
        config=DFMConfig(
            geometry_backend="occt_cpp_external",
            geometry_executable=os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"],
            geometry_timeout_seconds=120,
            geometry_job_poll_interval_ms=20,
        ),
        reconcile_jobs=False,
    )
    try:
        project_id = service.project("create", name="native editable range")[
            "project_id"
        ]
        source = (
            Path(__file__).resolve().parents[2]
            / "fixtures/dfm/step/injection_plate_with_hole.step"
        )
        service.project("add_input", project_id=project_id, path=str(source))
        for key, value in {
            "process": "injection",
            "model_units": "mm",
            "pull_dir": [0, 0, 1],
        }.items():
            service.project(
                "confirm_fact", project_id=project_id, fact_name=key, fact_value=value
            )
        service.analysis("discover", project_id=project_id)
        context = service.analysis("review_context", project_id=project_id)
        wall = next(
            feature for feature in context["features"] if feature["kind"] == "main_wall"
        )
        refs = wall["properties"]["geometry_refs"]
        assert len(refs) >= 2
        created = service.analysis(
            "restructure_forming_scopes",
            project_id=project_id,
            expected_revision=context["revision"],
            forming_scopes=[
                {
                    "forming_scope_id": "SIDE_TEST",
                    "kind": "undercut",
                    "mode": "local",
                    "geometry_refs": refs,
                }
            ],
        )
        assert created["status"] == "forming_scope_review_required"
        yield service, project_id, wall, refs
    finally:
        service.close()


def metadata(service, project_id):
    manifest = service._store(project_id).load()
    digest = manifest.inputs[0].sha256
    return (
        manifest,
        digest,
        manifest.capabilities["geometry_discovery"]["inputs"][digest],
    )


def test_review_snapshot_rebases_confirmation_without_losing_new_artifacts(
    model, monkeypatch
):
    from tools.dfm.contracts import ArtifactRecord

    service, project_id, wall, _refs = model
    context = service.analysis("review_context", project_id=project_id)
    group = next(
        row
        for row in context["review_groups"]
        if row["kind"] == "ordinary_structure"
        and wall["feature_id"] in row["target_refs"]
    )
    store = service._store(project_id)
    update = store.update
    monkeypatch.setattr(service, "_store", lambda _project_id: store)
    added = ArtifactRecord(
        "artifact_progress",
        "fixture_progress",
        "progress.json",
        "application/json",
        "now",
    )
    path = service.workspace.project_dir(project_id) / added.relative_path
    path.write_text("{}", encoding="utf-8")
    update(lambda current: replace(current, name="unrelated progress before request"))
    injected = False

    def while_committing(transform, expected_revision=None):
        nonlocal injected
        if not injected and transform.__name__ == "apply":
            injected = True
            update(
                lambda current: replace(
                    current,
                    artifacts=[*current.artifacts, added],
                    capabilities={
                        **current.capabilities,
                        "concurrent_progress": {"value": 1},
                    },
                )
            )
        return update(transform, expected_revision=expected_revision)

    monkeypatch.setattr(store, "update", while_committing)
    service.analysis(
        "confirm_discovery",
        project_id=project_id,
        expected_revision=context["revision"],
        expected_snapshot_id=context["snapshot_id"],
        review_group_ids=[group["review_group_id"]],
    )
    current = store.load()
    assert injected
    assert added in current.artifacts
    assert current.capabilities["concurrent_progress"] == {"value": 1}
    assert (
        next(
            row
            for row in current.review_groups
            if row.review_group_id == group["review_group_id"]
        ).status
        == "confirmed"
    )


def test_feature_face_removal_updates_worker_parts_and_requires_new_confirmation(model):
    service, project_id, wall, refs = model
    context = service.analysis("review_context", project_id=project_id)
    group = next(item for item in context["review_groups"]
                 if item["kind"] == "ordinary_structure" and wall["feature_id"] in item["target_refs"])
    service.analysis("confirm_discovery", project_id=project_id,
                     review_group_ids=[group["review_group_id"]], expected_revision=context["revision"])
    context = service.analysis("review_context", project_id=project_id)
    selected = refs[:1]
    saved = service.analysis("update_feature_geometry", project_id=project_id,
                            feature_id=wall["feature_id"], geometry_refs=selected,
                            expected_revision=context["revision"], expected_snapshot_id=context["snapshot_id"])
    assert saved["geometry_refs"] == selected
    current = service.analysis("review_context", project_id=project_id)
    edited = next(row for row in current["features"] if row["feature_id"] == wall["feature_id"])
    assert all(ref in selected for part in edited["properties"]["parts"].values() for ref in part)
    groups = [row for row in current["review_groups"] if row["kind"] == "ordinary_structure"
              and wall["feature_id"] in row["target_refs"]]
    assert len(groups) == 1
    assert groups[0]["status"] == "pending_review"
    assert groups[0]["review_group_id"] != group["review_group_id"]


def test_review_snapshot_edit_tolerates_progress_but_rejects_changed_geometry(
    model, monkeypatch
):
    service, project_id, _wall, refs = model
    context = service.analysis("review_context", project_id=project_id)
    prepare = service._prepare_geometry_edit

    def progress_before_intent(*args, **kwargs):
        service._store(project_id).update(
            lambda current: replace(current, name="progress before intent")
        )
        return prepare(*args, **kwargs)

    monkeypatch.setattr(service, "_prepare_geometry_edit", progress_before_intent)
    service.analysis(
        "revise_annotations",
        project_id=project_id,
        expected_revision=context["revision"],
        expected_snapshot_id=context["snapshot_id"],
        annotations=[
            {
                "annotation_id": "progress-safe",
                "kind": "appearance",
                "value": "appearance",
                "geometry_refs": refs[:1],
            }
        ],
    )
    before = service._store(project_id).load()
    assert before.geometry_edits[-1]["status"] == "committed"
    with pytest.raises(DFMError) as failure:
        service.analysis(
            "revise_annotations",
            project_id=project_id,
            expected_revision=context["revision"],
            expected_snapshot_id=context["snapshot_id"],
            annotations=[
                {
                    "annotation_id": "must-not-apply",
                    "kind": "appearance",
                    "value": "appearance",
                    "geometry_refs": refs[:1],
                }
            ],
        )
    assert failure.value.code == "geometry_edit_conflict"
    assert service._store(project_id).load() == before


def test_native_attributes_commit_independently_and_recover_edge_references(
    model, monkeypatch
):
    service, project_id, wall, refs = model
    context = service.analysis("review_context", project_id=project_id)
    assert {item["kind"] for item in context["annotation_kinds"]} >= {
        "appearance",
        "mold_side",
        "forming_side",
        "parting_boundary",
    }
    structure = next(
        row
        for row in context["review_groups"]
        if row["kind"] == "ordinary_structure"
        and wall["feature_id"] in row["target_refs"]
    )
    service.analysis(
        "confirm_discovery",
        project_id=project_id,
        expected_revision=context["revision"],
        review_group_ids=[structure["review_group_id"]],
    )
    manifest, digest, prior = metadata(service, project_id)
    old_head = prior["worker_snapshot_id"]
    opened = service._open_committed_geometry(project_id, manifest, manifest.inputs[0])
    topology = json.loads(
        Path(opened["artifacts"]["topology_map"]).read_text(encoding="utf-8")
    )
    edge_ref = topology["edges"][0]["geometry_ref"]
    assert edge_ref["kind"] == "edge"

    def no_replan(*args, **kwargs):
        pytest.fail("An unrelated attribute update must not replan geometric work")

    monkeypatch.setattr(service, "_after_workflow_update", no_replan)
    rows = [
        {
            "annotation_id": "appearance",
            "kind": "appearance",
            "value": "appearance",
            "geometry_refs": refs[:1],
        },
        {
            "annotation_id": "mold",
            "kind": "mold_side",
            "value": "moving",
            "geometry_refs": refs[:1],
        },
        {
            "annotation_id": "parting",
            "kind": "parting_boundary",
            "value": "parting",
            "geometry_refs": [edge_ref],
        },
    ]
    saved = service.analysis(
        "revise_annotations",
        project_id=project_id,
        expected_revision=manifest.revision,
        annotations=rows,
    )
    attributes = [
        group
        for group in saved["review_groups"]
        if group["kind"] == "surface_attribute"
    ]
    assert len(attributes) == 3
    assert (
        next(
            group
            for group in saved["review_groups"]
            if group["review_group_id"] == structure["review_group_id"]
        )["status"]
        == "confirmed"
    )
    assert (
        service.geometry_sessions.call(
            project_id, "snapshots.get", {"snapshot_id": old_head}
        )["annotations"]
        == []
    )
    selected = next(
        group for group in attributes if group["target_refs"] == ["appearance"]
    )
    service.analysis(
        "confirm_discovery",
        project_id=project_id,
        expected_revision=saved["revision"],
        review_group_ids=[selected["review_group_id"]],
    )
    current = service.analysis("review_context", project_id=project_id)
    assert (
        next(
            group
            for group in current["review_groups"]
            if group["target_refs"] == ["appearance"]
        )["status"]
        == "confirmed"
    )
    assert (
        next(
            group
            for group in current["review_groups"]
            if group["target_refs"] == ["mold"]
        )["status"]
        == "pending_review"
    )
    edited = service.analysis(
        "revise_annotations",
        project_id=project_id,
        expected_revision=current["revision"],
        annotations=[{"annotation_id": "appearance", "value": "non_appearance"}],
    )
    assert (
        next(
            group
            for group in edited["review_groups"]
            if group["target_refs"] == ["appearance"]
        )["status"]
        == "pending_review"
    )
    assert all(row["source"] == "user" for row in edited["annotations"])
    committed, _, _ = metadata(service, project_id)
    assert committed.geometry_edits[-1]["method"] == "annotations.revise"
    assert committed.geometry_edits[-1]["status"] == "committed"
    session = service.geometry_sessions.acquire(project_id).session
    session._process.terminate()
    session._process.wait(timeout=5)
    recovered = service._open_committed_geometry(
        project_id, committed, committed.inputs[0]
    )
    assert recovered["annotations"] == edited["annotations"]
    assert recovered["diagnostics"]["model_import_count"] == 1
    retired = service.analysis(
        "revise_annotations",
        project_id=project_id,
        expected_revision=committed.revision,
        annotations=[],
        retire_annotation_ids=["mold"],
    )
    assert not any(
        group["target_refs"] == ["mold"] for group in retired["review_groups"]
    )
    assert (
        next(row for row in retired["annotations"] if row["annotation_id"] == "mold")[
            "status"
        ]
        == "stale"
    )


def test_failed_attribute_revision_never_adopts_worker_or_project_state(model):
    service, project_id, _, refs = model
    manifest, digest, prior = metadata(service, project_id)
    old_head = prior["worker_snapshot_id"]
    with pytest.raises(DFMError) as error:
        service.analysis(
            "revise_annotations",
            project_id=project_id,
            expected_revision=manifest.revision,
            annotations=[
                {
                    "annotation_id": "wrong",
                    "kind": "mold_side",
                    "value": "core",
                    "geometry_refs": refs[:1],
                }
            ],
        )
    assert error.value.code == "annotation_revision_invalid"
    after, _, state = metadata(service, project_id)
    assert state["worker_snapshot_id"] == old_head
    assert state.get("annotations", []) == prior.get("annotations", [])
    assert after.geometry_edits[-1]["status"] == "failed"
    owned = service.geometry_sessions.acquire(project_id)
    assert owned.snapshots_by_input_sha256[digest]["snapshot_id"] == old_head


def raw_scope(service, project_id):
    return next(
        scope
        for scope in service.analysis("review_context", project_id=project_id)[
            "forming_scopes"
        ]
        if scope["forming_scope_id"] == "SIDE_TEST"
    )


def test_native_range_edit_commits_once_despite_progress_and_keeps_old_snapshot(
    model, monkeypatch
):
    service, project_id, wall, refs = model
    context = service.analysis("review_context", project_id=project_id)
    # Confirm the ordinary structure independently of the local candidate.
    group = next(
        row
        for row in context["review_groups"]
        if row["kind"] == "ordinary_structure"
        and wall["feature_id"] in row["target_refs"]
    )
    service.analysis(
        "confirm_discovery",
        project_id=project_id,
        expected_revision=context["revision"],
        review_group_ids=[group["review_group_id"]],
    )
    before, digest, prior = metadata(service, project_id)
    old_head = prior["worker_snapshot_id"]
    original = service.geometry_sessions.call

    def with_progress(pid, method, params):
        result = original(pid, method, params)
        if method == "forming_scopes.revise":
            pending = service._store(pid).load()
            assert pending.geometry_edits[-1]["status"] == "pending"
            assert (
                service.geometry_sessions.acquire(pid).snapshots_by_input_sha256[
                    digest
                ]["snapshot_id"]
                != result["snapshot_id"]
            )
            service._store(pid).update(
                lambda current: replace(current, name="progress changed")
            )
        return result

    monkeypatch.setattr(service.geometry_sessions, "call", with_progress)
    result = service.analysis(
        "update_forming_scope_geometry",
        project_id=project_id,
        forming_scope_id="SIDE_TEST",
        geometry_refs=refs[:1],
        expected_revision=before.revision,
    )
    after, _, current = metadata(service, project_id)
    assert result["revision"] == after.revision
    assert after.name == "progress changed"
    assert after.geometry_edits[-1]["status"] == "committed"
    assert raw_scope(service, project_id)["geometry_refs"] == refs[:1]
    assert raw_scope(service, project_id)["draw_direction"] is None
    assert raw_scope(service, project_id)["status"] == "candidate"
    ordinary = [
        row
        for row in service.analysis("review_context", project_id=project_id)[
            "review_groups"
        ]
        if row["review_group_id"] == group["review_group_id"]
    ]
    assert ordinary[0]["status"] == "confirmed"
    old = original(
        project_id,
        "snapshots.get",
        {
            "snapshot_id": service.geometry_sessions.resolve_snapshot_id(
                project_id, old_head
            )
        },
    )
    assert (
        next(
            row
            for row in old["forming_scopes"]
            if row["forming_scope_id"] == "SIDE_TEST"
        )["geometry_refs"]
        == refs
    )
    cached = service.geometry_sessions.acquire(project_id).snapshots_by_input_sha256[
        digest
    ]
    assert cached["snapshot_id"] == current["worker_snapshot_id"]
    assert cached["diagnostics"]["model_import_count"] == 1


def test_failed_native_geometry_commit_does_not_adopt_or_recover_the_orphan_branch(
    model, monkeypatch
):
    service, project_id, _, refs = model
    before, digest, prior = metadata(service, project_id)
    original_head = prior["worker_snapshot_id"]
    with monkeypatch.context() as patch:
        patch.setattr(
            GeometryEdit,
            "commit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            service.analysis(
                "update_forming_scope_geometry",
                project_id=project_id,
                forming_scope_id="SIDE_TEST",
                geometry_refs=refs[:1],
                expected_revision=before.revision,
            )
    after, _, current = metadata(service, project_id)
    assert current["worker_snapshot_id"] == original_head
    assert after.geometry_edits[-1]["status"] == "failed"
    assert raw_scope(service, project_id)["geometry_refs"] == refs
    crashed = service.geometry_sessions.acquire(project_id).session
    crashed._process.terminate()
    crashed._process.wait(timeout=5)
    recovered = service._open_committed_geometry(project_id, after, after.inputs[0])
    assert recovered["diagnostics"]["model_import_count"] == 1
    scope = next(
        row
        for row in recovered["forming_scopes"]
        if row["forming_scope_id"] == "SIDE_TEST"
    )
    assert scope["geometry_refs"] == refs
    assert service.geometry_sessions.acquire(project_id).session.pid != crashed.pid


def test_concurrent_native_scope_commit_wins_without_being_overwritten_by_a_late_branch(
    model, monkeypatch
):
    service, project_id, _, refs = model
    before, _, _ = metadata(service, project_id)
    prepared, release = Event(), Event()
    original = service.geometry_sessions.call
    blocked = False

    def delay_first(pid, method, params):
        nonlocal blocked
        result = original(pid, method, params)
        if method == "forming_scopes.revise" and not blocked:
            blocked = True
            prepared.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(service.geometry_sessions, "call", delay_first)
    with ThreadPoolExecutor(max_workers=1) as executor:
        late = executor.submit(
            service.analysis,
            "update_forming_scope_geometry",
            project_id=project_id,
            forming_scope_id="SIDE_TEST",
            geometry_refs=refs[:1],
            expected_revision=before.revision,
        )
        try:
            assert prepared.wait(5)
            revision = service._store(project_id).load().revision
            winner = service.analysis(
                "update_forming_scope_geometry",
                project_id=project_id,
                forming_scope_id="SIDE_TEST",
                geometry_refs=refs[-1:],
                expected_revision=revision,
            )
        finally:
            release.set()
        with pytest.raises(DFMError) as error:
            late.result(timeout=10)
    assert error.value.code == "geometry_edit_conflict"
    assert raw_scope(service, project_id)["geometry_refs"] == refs[-1:]
    after, digest, state = metadata(service, project_id)
    assert state["worker_snapshot_id"] == winner["snapshot"]["worker_snapshot_id"]
    assert (
        service.geometry_sessions.acquire(project_id).snapshots_by_input_sha256[digest][
            "snapshot_id"
        ]
        == state["worker_snapshot_id"]
    )
    assert sorted(row["status"] for row in after.geometry_edits[-2:]) == [
        "committed",
        "failed",
    ]


def test_acknowledged_geometry_recovers_after_worker_dies_before_head_adoption(
    model, monkeypatch
):
    service, project_id, _, refs = model
    before, _, prior = metadata(service, project_id)
    original_head = prior["worker_snapshot_id"]
    select = service.geometry_sessions.select_snapshot
    crashed_pid = None

    def crash_on_adoption(pid, digest, snapshot_id):
        nonlocal crashed_pid
        if snapshot_id != original_head and crashed_pid is None:
            session = service.geometry_sessions.acquire(pid).session
            crashed_pid = session.pid
            session._process.terminate()
            session._process.wait(timeout=5)
            raise DFMError("geometry_session_lost", "worker died after project commit")
        return select(pid, digest, snapshot_id)

    monkeypatch.setattr(service.geometry_sessions, "select_snapshot", crash_on_adoption)
    saved = service.analysis(
        "update_forming_scope_geometry",
        project_id=project_id,
        forming_scope_id="SIDE_TEST",
        geometry_refs=refs[:1],
        expected_revision=before.revision,
    )
    after, _, state = metadata(service, project_id)
    assert saved["ok"] is True
    assert after.geometry_edits[-1]["status"] == "committed"
    assert crashed_pid is not None
    recovered = service._open_committed_geometry(project_id, after, after.inputs[0])
    scope = next(
        row
        for row in recovered["forming_scopes"]
        if row["forming_scope_id"] == "SIDE_TEST"
    )
    assert scope["geometry_refs"] == refs[:1]
    assert recovered["snapshot_id"] == service.geometry_sessions.resolve_snapshot_id(
        project_id, state["worker_snapshot_id"]
    )
    assert service.geometry_sessions.acquire(project_id).session.pid != crashed_pid


def test_native_scope_split_retires_old_waiting_tasks_without_duplicating_features(
    model,
):
    service, project_id, _, refs = model
    planned = service.analysis("plan", project_id=project_id)
    assert "plan" in planned, planned
    previous_plan = planned["plan"]
    previous, _, _ = metadata(service, project_id)
    feature_ids = {feature.feature_id for feature in previous.features}
    split = service.analysis(
        "restructure_forming_scopes",
        project_id=project_id,
        expected_revision=previous.revision,
        retire_scope_ids=["SIDE_TEST"],
        forming_scopes=[
            {
                "forming_scope_id": "SIDE_A",
                "kind": "undercut",
                "mode": "local",
                "geometry_refs": refs[:1],
            },
            {
                "forming_scope_id": "SIDE_B",
                "kind": "undercut",
                "mode": "local",
                "geometry_refs": refs[-1:],
            },
        ],
    )
    scopes = {row["forming_scope_id"]: row for row in split["forming_scopes"]}
    assert scopes["SIDE_TEST"]["status"] == "stale"
    assert scopes["SIDE_A"]["supersedes_scope_ids"] == ["SIDE_TEST"]
    assert scopes["SIDE_B"]["supersedes_scope_ids"] == ["SIDE_TEST"]
    after, _, _ = metadata(service, project_id)
    assert {feature.feature_id for feature in after.features} == feature_ids
    assert all(
        row.get("to_ref") != "SIDE_TEST" for row in split["snapshot"]["relationships"]
    )
    rebuilt = service.analysis(
        "plan", project_id=project_id, base_plan_id=previous_plan["plan_id"]
    )
    assert "plan" in rebuilt, rebuilt
    from tools.dfm.contracts import PlanRecord

    plan = PlanRecord.from_dict(rebuilt["plan"])
    active = plan.active_plan_fragments()
    scope_ids = {
        item.context.get("forming_scope_id")
        for fragment in active
        for item in fragment.items
    }
    assert "SIDE_TEST" not in scope_ids
    assert {"SIDE_A", "SIDE_B"} <= scope_ids
    old_ids = {row["fragment_id"] for row in previous_plan["plan_fragments"]}
    assert any(
        fragment.status == "stale" and fragment.supersedes in old_ids
        for fragment in plan.plan_fragments
    )


def test_native_range_edit_invalidates_only_its_scope_reviews(model):
    service, project_id, wall, refs = model
    context = service.analysis("review_context", project_id=project_id)
    selected = [
        group["review_group_id"]
        for group in context["review_groups"]
        if group["status"] == "pending_review"
        and group["kind"] in {"ordinary_structure", "product_forming", "undercut_scope"}
    ]
    service.analysis(
        "confirm_discovery",
        project_id=project_id,
        expected_revision=context["revision"],
        review_group_ids=selected,
    )
    context = service.analysis("review_context", project_id=project_id)
    local = next(
        group
        for group in context["review_groups"]
        if group["kind"] == "local_structure" and "SIDE_TEST" in group["target_refs"]
    )
    service.analysis(
        "confirm_discovery",
        project_id=project_id,
        expected_revision=context["revision"],
        review_group_ids=[local["review_group_id"]],
    )
    context = service.analysis("review_context", project_id=project_id)
    scope = next(
        row
        for row in context["forming_scopes"]
        if row["forming_scope_id"] == "SIDE_TEST"
    )
    confirmed = service.analysis(
        "revise_forming_scopes",
        project_id=project_id,
        expected_revision=context["revision"],
        forming_scopes=[
            {
                key: scope[key]
                for key in ("forming_scope_id", "kind", "mode", "geometry_refs")
            }
            | {
                "draw_direction": [1, 0, 0],
                "coordinate_system": "model",
                "direction_source": "user_confirmed",
            }
        ],
    )
    result = service.analysis(
        "update_forming_scope_geometry",
        project_id=project_id,
        forming_scope_id="SIDE_TEST",
        geometry_refs=refs[:1],
        expected_revision=confirmed["revision"],
    )
    state = service._store(project_id).load()
    assert any(
        group.kind == "ordinary_structure"
        and group.status == "confirmed"
        and wall["feature_id"] in group.target_refs
        for group in state.review_groups
    )
    assert any(
        group.kind == "local_structure"
        and group.review_group_id == local["review_group_id"]
        and group.status == "stale"
        for group in state.review_groups
    )
    assert any(
        group.kind == "local_direction"
        and "SIDE_TEST" in group.target_refs
        and group.status == "stale"
        for group in state.review_groups
    )
    assert any(
        group.kind == "product_forming"
        and "MAIN" in group.target_refs
        and group.status == "confirmed"
        for group in state.review_groups
    )
    assert (
        next(
            row
            for row in result["forming_scopes"]
            if row["forming_scope_id"] == "SIDE_TEST"
        )["draw_direction"]
        is None
    )


def test_expired_snapshot_is_replayed_without_reupload_or_adopting_an_orphan(model):
    service, project_id, _, refs = model
    manifest, digest, state = metadata(service, project_id)
    owned = service.geometry_sessions.acquire(project_id)
    before = owned.snapshots_by_input_sha256[digest]["snapshot_id"]
    owned.session.call("snapshots.close", {"snapshot_id": before})
    restored = service._open_committed_geometry(
        project_id, manifest, manifest.inputs[0]
    )
    assert restored["snapshot_id"] != before
    assert (
        service.geometry_sessions.resolve_snapshot_id(
            project_id, state["worker_snapshot_id"]
        )
        == restored["snapshot_id"]
    )
    scope = next(
        row
        for row in restored["forming_scopes"]
        if row["forming_scope_id"] == "SIDE_TEST"
    )
    assert scope["geometry_refs"] == refs
    assert scope["draw_direction"] is None


@pytest.mark.parametrize(
    "direction", [[0, 0, 0], [float("nan"), 0, 1], [0, float("inf"), 1], [True, 0, 1]]
)
def test_invalid_direction_is_rejected_before_creating_a_geometry_intent(
    model, direction
):
    service, project_id, _, refs = model
    before, _, _ = metadata(service, project_id)
    with pytest.raises(DFMError) as error:
        service.analysis(
            "revise_forming_scopes",
            project_id=project_id,
            expected_revision=before.revision,
            forming_scopes=[
                {
                    "forming_scope_id": "SIDE_TEST",
                    "kind": "undercut",
                    "mode": "local",
                    "geometry_refs": refs,
                    "coordinate_system": "model",
                    "direction_source": "user_confirmed",
                    "draw_direction": direction,
                }
            ],
        )
    assert error.value.code == "direction_invalid"
    assert service._store(project_id).load().geometry_edits == before.geometry_edits
