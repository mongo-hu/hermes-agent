from dataclasses import replace
from unittest.mock import Mock

import pytest

from tools.dfm.contracts import FactRecord, InputRecord
from tools.dfm.errors import DFMError
from tools.dfm.project.geometry_edits import GeometryEdit, geometry_identity
from tools.dfm.project.manifest import ManifestStore
from tools.dfm.project.workspace import DFMWorkspace


@pytest.fixture
def project(tmp_path):
    workspace = DFMWorkspace(tmp_path / "dfm")
    manifest = workspace.create_project("geometry transactions")
    store = ManifestStore(workspace.project_dir(manifest.project_id))
    store.update(
        lambda current: replace(
            current,
            inputs=[
                InputRecord(
                    "input",
                    "step",
                    "model.step",
                    "inputs/model.step",
                    1,
                    "a" * 64,
                    "now",
                )
            ],
            capabilities={
                "geometry_discovery": {
                    "inputs": {"a" * 64: {"worker_snapshot_id": "base"}}
                }
            },
        )
    )
    return store


def begin(store):
    return GeometryEdit.begin(
        store,
        expected_revision=store.load().revision,
        input_sha256="a" * 64,
        method="features.revise",
        params={"snapshot_id": "base", "revisions": []},
    )


def head(current, snapshot_id):
    capabilities = {
        **current.capabilities,
        "geometry_discovery": {
            "inputs": {"a" * 64: {"worker_snapshot_id": snapshot_id}}
        },
    }
    return replace(current, capabilities=capabilities)


def test_intent_is_durable_before_worker_call_and_commit_tolerates_unrelated_updates(
    project,
):
    edit = begin(project)
    pending = ManifestStore(project.project_dir).load()
    assert pending.geometry_edits[0]["status"] == "pending"
    assert pending.geometry_edits[0]["base_snapshot_id"] == "base"
    project.update(
        lambda current: replace(
            current,
            name="renamed during calculation",
            facts=[FactRecord("material", "material", "ABS", "USR", "confirmed")],
        )
    )
    committed = edit.commit("next", lambda current: head(current, "next"))
    assert committed.name == "renamed during calculation"
    assert committed.facts[0].value == "ABS"
    assert committed.geometry_edits[0]["status"] == "committed"
    assert committed.geometry_edits[0]["result_snapshot_id"] == "next"


def test_stale_user_revision_cannot_start_an_edit(project):
    revision = project.load().revision
    project.update(lambda current: replace(current, name="new"))
    with pytest.raises(DFMError) as error:
        GeometryEdit.begin(
            project,
            expected_revision=revision,
            input_sha256="a" * 64,
            method="features.revise",
            params={"snapshot_id": "base", "revisions": []},
        )
    assert error.value.code == "manifest_conflict"
    assert project.load().geometry_edits == []


@pytest.mark.parametrize("kind", ["progress", "snapshot", "units"])
def test_snapshot_guard_compares_geometry_at_intent_commit_not_just_revision(
    project, kind
):
    before = project.load()
    expected_identity = geometry_identity(before, "a" * 64)
    if kind == "progress":
        project.update(lambda current: replace(current, name="new progress"))
    elif kind == "snapshot":
        project.update(lambda current: head(current, "another-geometry"))
    else:
        project.update(
            lambda current: replace(
                current,
                facts=[FactRecord("units", "model_units", "inch", "USR", "confirmed")],
            )
        )

    def start():
        return GeometryEdit.begin(
            project,
            expected_revision=before.revision,
            input_sha256="a" * 64,
            method="features.revise",
            params={"snapshot_id": "base", "revisions": []},
            expected_identity=expected_identity,
        )

    if kind == "progress":
        assert start().manifest.name == "new progress"
        assert project.load().geometry_edits[-1]["status"] == "pending"
    else:
        with pytest.raises(DFMError) as error:
            start()
        assert error.value.code == "geometry_edit_conflict"
        assert project.load().geometry_edits == []


@pytest.mark.parametrize(
    "change",
    [
        lambda current: head(current, "other"),
        lambda current: replace(
            current,
            facts=[FactRecord("units", "model_units", "inch", "USR", "confirmed")],
        ),
    ],
)
def test_geometry_conflict_never_calls_the_projection_transform(project, change):
    edit = begin(project)
    latest = project.update(change)
    transform = Mock(side_effect=AssertionError("must not overwrite new geometry"))
    with pytest.raises(DFMError) as error:
        edit.commit("orphan", transform)
    assert error.value.code == "geometry_edit_conflict"
    transform.assert_not_called()
    edit.fail(error.value)
    recovered = ManifestStore(project.project_dir).load()
    assert recovered.capabilities == latest.capabilities
    assert recovered.facts == latest.facts
    assert recovered.geometry_edits[0]["status"] == "failed"


def test_failed_commit_does_not_publish_a_new_snapshot(project, monkeypatch):
    edit = begin(project)
    monkeypatch.setattr(
        "tools.dfm.project.manifest.journal.append",
        Mock(side_effect=OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        edit.commit("orphan", lambda current: head(current, "orphan"))
    recovered = project.load()
    assert (
        recovered.capabilities["geometry_discovery"]["inputs"]["a" * 64][
            "worker_snapshot_id"
        ]
        == "base"
    )
    assert recovered.geometry_edits[0]["status"] == "pending"
