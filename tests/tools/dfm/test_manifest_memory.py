"""Committed session reads use memory without weakening durable revisions."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tools.dfm.contracts import ProjectManifest
from tools.dfm.errors import DFMError
from tools.dfm.project import manifest as module
from tools.dfm.project.manifest import ManifestStore


def project():
    return ProjectManifest("project-memory", "part", "now", "now")


def test_repeated_instances_read_committed_memory_without_reading_json(tmp_path, monkeypatch):
    store = ManifestStore(tmp_path / "project")
    store.save(project())
    original = Path.read_text

    def no_manifest_read(path, *args, **kwargs):
        if path == store.path:
            raise AssertionError("a cache hit must not read the full manifest")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_manifest_read)
    assert ManifestStore(store.project_dir).load().name == "part"
    changed = store.update(lambda current: replace(current, name="reviewed"))
    assert changed.revision == 1
    assert ManifestStore(store.project_dir).load().name == "reviewed"


def test_reads_and_save_arguments_cannot_mutate_the_committed_state(tmp_path):
    store = ManifestStore(tmp_path)
    source = replace(project(), capabilities={"test": {"value": [1]}})
    store.save(source)
    source.capabilities["test"]["value"].append(2)
    read = store.load()
    read.capabilities["test"]["value"].append(3)
    assert store.load().capabilities == {"test": {"value": [1]}}


def test_atomic_external_write_invalidates_memory_and_enforces_revision(tmp_path):
    store = ManifestStore(tmp_path)
    store.save(project())
    external = replace(project(), name="external", revision=9)
    replacement = tmp_path / "external.json"
    replacement.write_text(json.dumps(external.to_dict()), encoding="utf-8")
    replacement.replace(store.path)
    assert store.load().name == "external"
    with pytest.raises(DFMError, match="changed") as error:
        store.update(lambda current: current, expected_revision=0)
    assert error.value.code == "manifest_conflict"
    assert store.update(lambda current: current, expected_revision=9).revision == 10


def test_deleted_checkpoint_never_returns_stale_memory(tmp_path):
    store = ManifestStore(tmp_path)
    store.save(project())
    store.path.unlink()
    with pytest.raises(DFMError) as error:
        store.load()
    assert error.value.code == "manifest_invalid"


def test_process_cache_loss_restores_the_committed_project(tmp_path):
    store = ManifestStore(tmp_path)
    store.save(project())
    store.update(lambda current: replace(current, name="confirmed"))
    with module._LOCKS_GUARD:
        module._STATES.pop(store.path, None)
    restored = ManifestStore(tmp_path).load()
    assert (restored.name, restored.revision) == ("confirmed", 1)


def test_failed_disk_commit_does_not_publish_new_memory(tmp_path, monkeypatch):
    store = ManifestStore(tmp_path)
    store.save(project())

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(module.journal, "append", fail)
    with pytest.raises(OSError, match="disk full"):
        store.update(lambda current: replace(current, name="not committed"))
    assert store.load().name == "part"
    assert store.load().revision == 0
    assert not list(tmp_path.glob(".*.tmp"))


def test_cache_is_bounded_and_evicted_projects_remain_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_MAX_CACHED_PROJECTS", 2)
    stores = [ManifestStore(tmp_path / str(index)) for index in range(3)]
    for index, store in enumerate(stores):
        store.save(replace(project(), name=str(index)))
    with module._LOCKS_GUARD:
        assert len(module._STATES) <= 2
    assert stores[0].load().name == "0"
