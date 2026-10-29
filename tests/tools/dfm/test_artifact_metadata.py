"""Actual file I/O proves metadata reuse without retaining a second mesh."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.dfm.contracts import ArtifactRecord
from tools.dfm.errors import DFMError
from tools.dfm.geometry import artifact_metadata as cache
from tools.dfm.feature_recognition.occt_cpp import OCCTCppFeatureRecognitionProvider
from tools.dfm.runtime.jobs import JobManager
from tools.dfm.viewer import _read


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear_geometry_file_cache()
    yield
    cache.clear_geometry_file_cache()


def scene(path, identity="mesh-a", *, count=10000):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "input_sha256": "a" * 64,
        "render_mesh_snapshot": {
            "render_mesh_snapshot_id": identity,
            "topology_snapshot_id": "topology",
        },
        "primitives": [
            {"vertices": [[1.0, 2.0, 3.0]] * count, "triangles": [[0, 1, 2]] * count}
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def descriptor(path, root):
    return ArtifactRecord(
        "scene",
        "render_scene",
        path.relative_to(root).as_posix(),
        "application/json",
        "now",
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_shared_consumers_hash_and_parse_once_then_only_use_small_metadata(
    tmp_path, monkeypatch
):
    path = scene(tmp_path / "scene.json")
    loads, parses = [], []
    load, parse = cache._load_geometry_file, cache._parse_header

    def observed_load(*args):
        loads.append(args[0])
        return load(*args)

    def observed_parse(*args):
        parses.append(len(args[0]))
        return parse(*args)

    monkeypatch.setattr(cache, "_load_geometry_file", observed_load)
    monkeypatch.setattr(cache, "_parse_header", observed_parse)
    provider = OCCTCppFeatureRecognitionProvider(None)
    artifact = provider._artifact(tmp_path, "a" * 64, "render_scene", path)
    for _ in range(12):
        checked = JobManager._validate_artifact(
            tmp_path, artifact, allow_cached_geometry=True
        )
        metadata = _read(tmp_path, checked)
        assert metadata["render_mesh_snapshot"]["render_mesh_snapshot_id"] == "mesh-a"
        assert "primitives" not in metadata
    assert loads == [path]
    assert len(parses) == 1 and parses[0] == path.stat().st_size
    assert len(cache.inspect_geometry_file(path, "render_scene")._metadata_json) < 1024
    # Default validation is deliberately fresh, but unchanged JSON is not reparsed.
    JobManager._validate_artifact(tmp_path, artifact)
    assert len(loads) == 2
    assert len(parses) == 1


def test_normal_change_refreshes_metadata_and_rejects_old_descriptor(tmp_path):
    path = scene(tmp_path / "scene.json", count=10)
    old = descriptor(path, tmp_path)
    before = cache.inspect_geometry_file(path, "render_scene")
    scene(path, "mesh-b", count=11)
    after = cache.inspect_geometry_file(path, "render_scene")
    assert before.sha256 != after.sha256
    assert after.metadata["render_mesh_snapshot"]["render_mesh_snapshot_id"] == "mesh-b"
    with pytest.raises(DFMError) as error:
        JobManager._validate_artifact(tmp_path, old, allow_cached_geometry=True)
    assert error.value.code == "artifact_invalid"


def test_atomic_replacement_with_same_size_and_mtime_is_not_a_cache_hit(tmp_path):
    path = scene(tmp_path / "scene.json", count=10)
    original = cache.inspect_geometry_file(path, "render_scene")
    stamp = path.stat()
    replacement = scene(tmp_path / "replacement.json", "mesh-b", count=10)
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_size == stamp.st_size
    assert cache.inspect_geometry_file(path, "render_scene").sha256 != original.sha256


def test_forced_validation_never_trusts_an_unchanged_signature(tmp_path, monkeypatch):
    path = scene(tmp_path / "scene.json", count=10)
    old = descriptor(path, tmp_path)
    signature = cache._stat_signature(path.stat())
    cache.inspect_geometry_file(path, "render_scene")
    # Model a metadata signature that cannot reveal the content change.
    monkeypatch.setattr(cache, "_stat_signature", lambda _: signature)
    scene(path, "mesh-b", count=10)
    with pytest.raises(DFMError) as error:
        JobManager._validate_artifact(tmp_path, old)
    assert error.value.code == "artifact_invalid"


def test_file_changed_during_read_is_not_published_to_cache(tmp_path, monkeypatch):
    path = scene(tmp_path / "scene.json", count=10)
    parse = cache._parse_header

    def changed(content, kind):
        result = parse(content, kind)
        scene(path, "changed", count=20)
        return result

    monkeypatch.setattr(cache, "_parse_header", changed)
    with pytest.raises(DFMError, match="stable artifact"):
        cache.inspect_geometry_file(path, "render_scene")
    assert not cache._CACHE


def test_lru_budget_and_absolute_paths_prevent_cross_project_reuse(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cache, "_MAX_ENTRIES", 2)
    a, b, c = [
        scene(tmp_path / project / "scene.json", f"mesh-{project}", count=1)
        for project in ("a", "b", "c")
    ]
    assert (
        cache.inspect_geometry_file(a, "render_scene").metadata
        != cache.inspect_geometry_file(b, "render_scene").metadata
    )
    cache.inspect_geometry_file(a, "render_scene")
    cache.inspect_geometry_file(c, "render_scene")
    assert len(cache._CACHE) == 2
    assert (os.path.normcase(str(b.resolve())), "render_scene") not in cache._CACHE


def test_metadata_is_a_private_copy_even_for_concurrent_readers(tmp_path):
    path = scene(tmp_path / "scene.json", count=1)
    cache.inspect_geometry_file(path, "render_scene")

    def edit(_):
        value = cache.inspect_geometry_file(path, "render_scene").metadata
        value["render_mesh_snapshot"]["render_mesh_snapshot_id"] = "edited"
        return value

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(list(pool.map(edit, range(12)))) == 12
    assert (
        cache.inspect_geometry_file(path, "render_scene").metadata[
            "render_mesh_snapshot"
        ]["render_mesh_snapshot_id"]
        == "mesh-a"
    )


@pytest.mark.parametrize(
    "payload", [[], {"render_mesh_snapshot": {"oversized": "x" * 70000}}]
)
def test_invalid_or_oversized_metadata_is_not_cached(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DFMError):
        cache.inspect_geometry_file(path, "render_scene")
    assert not cache._CACHE


def test_viewer_project_boundary_is_checked_before_a_cache_hit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    path = scene(tmp_path / "outside.json", count=1)
    cache.inspect_geometry_file(path, "render_scene")
    artifact = ArtifactRecord(
        "outside", "render_scene", "../outside.json", "application/json", "now"
    )
    with pytest.raises(DFMError) as error:
        _read(project, artifact)
    assert error.value.code == "viewer_input_invalid"
