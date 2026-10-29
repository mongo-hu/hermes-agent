from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.dfm.runtime import result_documents as documents


@pytest.fixture(autouse=True)
def isolated_cache():
    documents.clear_result_document_cache()
    yield
    documents.clear_result_document_cache()


def table(value=2):
    return {
        "contract_version": "dfm.geometry/v2",
        "run_id": "run_fixture",
        "input_sha256": "a" * 64,
        "results": [{"feature_id": "F", "geometric_id": "G_FIXTURE", "value": value}],
    }


def test_writer_warms_a_canonical_private_projection_without_consumer_file_reads(
    tmp_path, monkeypatch
):
    path = tmp_path / "geometric_results.json"
    payload = table((2, 3))
    size, digest = documents.write_result_document(path, "geometric_results", payload)
    raw = path.read_bytes()
    assert (size, digest) == (len(raw), hashlib.sha256(raw).hexdigest())
    payload["results"][0]["value"] = 999
    original_open = Path.open

    def forbid_read(file, *args, **kwargs):
        if file.resolve() == path.resolve():
            raise AssertionError("unchanged working table was read again")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_read)
    for _ in range(12):
        row = documents.read_result_document(
            path, "geometric_results", sha256=digest, size_bytes=size
        )
        assert row["results"][0]["value"] == [2, 3]
        row["results"][0]["value"].clear()
    with ThreadPoolExecutor(max_workers=4) as executor:
        snapshots = list(
            executor.map(
                lambda _: documents.read_result_document(path, "geometric_results"),
                range(12),
            )
        )
    assert all(row["results"][0]["value"] == [2, 3] for row in snapshots)
    assert len({id(row["results"]) for row in snapshots}) == 12


def test_new_table_version_replaces_rows_and_its_checksum(tmp_path):
    path = tmp_path / "geometric_results.json"
    _size, old_digest = documents.write_result_document(
        path, "geometric_results", table(2)
    )
    size, digest = documents.write_result_document(path, "geometric_results", table(3))
    assert documents.read_result_document(
        path, "geometric_results", sha256=digest, size_bytes=size
    ) == table(3)
    with pytest.raises(ValueError):
        documents.read_result_document(path, "geometric_results", sha256=old_digest)


def test_cold_read_is_hydrated_once_and_external_changes_are_detected(
    tmp_path, monkeypatch
):
    path = tmp_path / "geometric_results.json"
    path.write_text(json.dumps(table(2)), encoding="utf-8")
    reads = []
    original = documents._read_entry

    def count(*args):
        reads.append(args[0])
        return original(*args)

    monkeypatch.setattr(documents, "_read_entry", count)
    for _ in range(6):
        assert documents.read_result_document(path, "geometric_results") == table(2)
    assert reads == [path]
    stamp = path.stat()
    path.write_text(json.dumps(table(3)), encoding="utf-8")
    # Fast same-size writes can share a Windows filesystem timestamp tick.
    # This case tests signature invalidation; forged signatures use force=True below.
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
    assert documents.read_result_document(path, "geometric_results") == table(3)
    assert reads == [path, path]


def test_atomic_replacement_with_same_size_and_mtime_is_not_reused(tmp_path):
    path = tmp_path / "geometric_results.json"
    documents.write_result_document(path, "geometric_results", table(2))
    stamp = path.stat()
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(path.read_bytes().replace(b'"value": 2', b'"value": 3'))
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(replacement, path)
    assert documents.read_result_document(path, "geometric_results") == table(3)


def test_forced_sealing_rejects_tampering_even_when_signature_looks_unchanged(
    tmp_path, monkeypatch
):
    path = tmp_path / "geometric_results.json"
    _size, digest = documents.write_result_document(path, "geometric_results", table(2))
    signature = documents._signature(path.stat())
    path.write_bytes(path.read_bytes().replace(b'"value": 2', b'"value": 3'))
    monkeypatch.setattr(documents, "_signature", lambda _stat: signature)
    with pytest.raises(ValueError):
        documents.read_result_document(
            path, "geometric_results", sha256=digest, force=True
        )
    assert not documents._CACHE
    with pytest.raises(ValueError):
        documents.read_result_document(path, "geometric_results", sha256=digest)


def test_lru_and_size_budget_keep_full_uncached_documents_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "_MAX_ENTRIES", 2)
    for index in range(3):
        documents.write_result_document(
            tmp_path / f"{index}.json", "geometric_results", table(index)
        )
    assert len(documents._CACHE) == 2
    assert not any(key[0].endswith("0.json") for key in documents._CACHE)
    monkeypatch.setattr(documents, "_MAX_DOCUMENT_BYTES", 1)
    path = tmp_path / "large.json"
    expected = table(list(range(100)))
    documents.write_result_document(path, "geometric_results", expected)
    assert documents.read_result_document(path, "geometric_results") == expected
    assert not any(key[0].endswith("large.json") for key in documents._CACHE)


def test_same_filename_in_distinct_projects_does_not_share_data(tmp_path):
    for project, value in [("a", 2), ("b", 3)]:
        folder = tmp_path / project
        folder.mkdir()
        documents.write_result_document(
            folder / "geometric_results.json", "geometric_results", table(value)
        )
    assert documents.read_result_document(
        tmp_path / "a/geometric_results.json", "geometric_results"
    ) == table(2)
    assert documents.read_result_document(
        tmp_path / "b/geometric_results.json", "geometric_results"
    ) == table(3)


def test_aggregate_budget_evicts_by_encoded_size_not_only_entry_count(
    tmp_path, monkeypatch
):
    sample_size = len(
        json.dumps(table(2), ensure_ascii=False, indent=2).encode("utf-8")
    )
    monkeypatch.setattr(documents, "_MAX_ENCODED_BYTES", sample_size * 2)
    for index in range(3):
        documents.write_result_document(
            tmp_path / f"{index}.json", "geometric_results", table(index)
        )
    assert len(documents._CACHE) == 2
    assert documents._ENCODED_BYTES == sample_size * 2
    assert not any(key[0].endswith("0.json") for key in documents._CACHE)
    assert documents.read_result_document(
        tmp_path / "0.json", "geometric_results"
    ) == table(0)
    assert documents._ENCODED_BYTES <= documents._MAX_ENCODED_BYTES


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_record_values_are_rejected_before_writing(tmp_path, value):
    path = tmp_path / "geometric_results.json"
    with pytest.raises(ValueError):
        documents.write_result_document(path, "geometric_results", table(value))
    assert not path.exists()
    assert not documents._CACHE


def test_failed_atomic_write_keeps_previous_table_and_memory_version(
    tmp_path, monkeypatch
):
    path = tmp_path / "evaluations.json"
    previous = {"evaluations": [{"outcome": "fail"}]}
    documents.write_result_document(path, "evaluations", previous)
    monkeypatch.setattr(
        documents.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(OSError("fixture rename failure")),
    )
    with pytest.raises(OSError):
        documents.write_result_document(path, "evaluations", {"evaluations": []})
    assert documents.read_result_document(path, "evaluations", force=True) == previous
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "payload", [[], {}, {"results": []}, {"evaluations": float("nan")}]
)
def test_invalid_evaluation_roots_are_never_cached(tmp_path, payload):
    path = tmp_path / "evaluations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        documents.read_result_document(path, "evaluations")
    assert not documents._CACHE


def test_large_geometry_kinds_are_not_accepted(tmp_path):
    path = tmp_path / "scene.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        documents.read_result_document(path, "render_scene")
