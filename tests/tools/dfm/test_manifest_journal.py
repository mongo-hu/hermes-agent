"""Crash recovery and bounded I/O through the real project-state entry point."""

import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace

import pytest

from tools.dfm.contracts import ProjectManifest
from tools.dfm.errors import DFMError
from tools.dfm.project import journal
from tools.dfm.project import manifest as module
from tools.dfm.project.manifest import ManifestStore


def create(tmp_path):
    store = ManifestStore(tmp_path / "project")
    store.save(ProjectManifest("journal-project", "part", "now", "now"))
    return store


def cold(store):
    with module._LOCKS_GUARD:
        module._STATES.pop(store.path, None)
    return ManifestStore(store.project_dir).load()


def test_small_updates_do_not_rewrite_the_full_checkpoint(tmp_path):
    store = create(tmp_path)
    before = store.path.read_bytes()
    store.update(lambda current: replace(current, name="reviewed"))
    assert store.path.read_bytes() == before
    assert len(store.journal_path.read_bytes().splitlines()) == 1
    assert cold(store).name == "reviewed"


def test_deltas_patch_only_changed_records_and_preserve_order_and_removals():
    before = {"project_id": "p", "revision": 0, "features": [
        {"feature_id": str(index), "properties": {"large": "x" * 2000}}
        for index in range(1500)
    ], "runs": [{"run_id": "run", "progress": 1, "plan_snapshot": {"large": "y" * 200000}}]}
    after = {**before, "revision": 1, "runs": [{**before["runs"][0], "progress": 2}]}
    encoded = journal.encode(before, after)
    assert len(encoded) < 1024
    assert journal.apply(before, json.loads(encoded)["changes"]) == after
    reordered = {**after, "features": [before["features"][2], {"feature_id": "new", "properties": {"note": "用户修订"}}, before["features"][0]]}
    assert journal.apply(after, journal.difference(after, reordered)) == reordered
    assert before["runs"][0]["progress"] == 1


def test_responded_commit_survives_abrupt_process_exit(tmp_path):
    store = create(tmp_path)
    script = """
import os, sys
from pathlib import Path
from dataclasses import replace
from tools.dfm.project.manifest import ManifestStore
store = ManifestStore(Path(sys.argv[1]))
store.update(lambda current: replace(current, name='confirmed before process exit'))
os._exit(0)
"""
    completed = subprocess.run([sys.executable, "-B", "-c", script, str(store.project_dir)],
        cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    restored = cold(store)
    assert (restored.revision, restored.name) == (1, "confirmed before process exit")
    assert json.loads(store.path.read_text(encoding="utf-8"))["revision"] == 0


def test_incomplete_last_event_is_ignored_and_removed_before_next_commit(tmp_path):
    store = create(tmp_path)
    store.update(lambda current: replace(current, name="committed"))
    with store.journal_path.open("ab") as stream:
        stream.write(b'{"partially_written":')
        stream.flush()
        os.fsync(stream.fileno())
    assert cold(store).name == "committed"
    store.update(lambda current: replace(current, name="next"))
    restored = cold(store)
    assert (restored.revision, restored.name) == (2, "next")
    assert len(store.journal_path.read_bytes().splitlines()) == 2


def test_corrupt_complete_event_is_not_treated_as_a_successful_old_state(tmp_path):
    store = create(tmp_path)
    store.update(lambda current: replace(current, name="committed"))
    event = json.loads(store.journal_path.read_bytes())
    event["revision"] = 9
    store.journal_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(DFMError) as error:
        cold(store)
    assert error.value.code == "manifest_journal_invalid"


def test_failed_append_rolls_back_without_publishing_memory(tmp_path, monkeypatch):
    store = create(tmp_path)
    original = os.fsync
    calls = []
    def fail_once(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError("simulated write failure")
        original(fd)
    monkeypatch.setattr(journal.os, "fsync", fail_once)
    with pytest.raises(OSError):
        store.update(lambda current: replace(current, name="not committed"))
    assert cold(store).revision == 0
    assert store.load().name == "part"


def test_checkpoint_rotation_and_failure_keep_every_committed_revision(tmp_path, monkeypatch):
    store = create(tmp_path)
    monkeypatch.setattr(module, "_CHECKPOINT_COMMITS", 2)
    store.update(lambda current: replace(current, name="one"))
    original = store._write_atomic
    def fail_rotation(path, payload):
        if path == store.journal_path:
            raise OSError("simulated log rotation failure")
        original(path, payload)
    monkeypatch.setattr(store, "_write_atomic", fail_rotation)
    assert store.update(lambda current: replace(current, name="two")).revision == 2
    assert cold(store).name == "two"
    monkeypatch.setattr(store, "_write_atomic", original)
    store.update(lambda current: replace(current, name="three"))
    sealed = store.checkpoint()
    assert sealed.revision == 3
    assert json.loads(store.path.read_text(encoding="utf-8"))["name"] == "three"
    assert store.journal_path.read_bytes() == b""
    assert cold(store).revision == 3


def test_two_processes_serialize_updates_and_observe_each_other(tmp_path):
    store = create(tmp_path)
    script = """
import sys
from pathlib import Path
from dataclasses import replace
from tools.dfm.project.manifest import ManifestStore
store = ManifestStore(Path(sys.argv[1]))
for index in range(4):
    name = sys.argv[2] + str(index)
    store.update(lambda current: replace(current, capabilities={**current.capabilities, name: {'committed': True}}))
"""
    children = [subprocess.Popen([sys.executable, "-B", "-c", script, str(store.project_dir), label],
        cwd=Path(__file__).resolve().parents[3], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for label in ("a", "b")]
    for child in children:
        _stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
    restored = cold(store)
    assert restored.revision == 8
    assert set(restored.capabilities) == {f"{label}{index}" for label in ("a", "b") for index in range(4)}
