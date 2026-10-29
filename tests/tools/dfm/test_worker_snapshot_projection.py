from dataclasses import replace

import pytest

from tools.dfm.contracts import FeatureRecord, InputRecord
from tools.dfm.errors import DFMError
from tools.dfm.feature_recognition.base import reconcile_worker_features
from tools.dfm.feature_recognition.occt_cpp import OCCTCppFeatureRecognitionProvider


def test_worker_projection_keeps_face_attributes_parts_and_scope_relationships():
    provider = OCCTCppFeatureRecognitionProvider()
    input_record = InputRecord("input", "step", "a.step", "inputs/a.step", 1, "a" * 64, "now")
    ref = {"kind": "face", "index": 1}
    relation = {"relation_id": "r1", "type": "intersects_forming_scope", "from_ref": "BOSS_7", "to_ref": "SIDE_1"}
    state = {
        "snapshot_id": "snapshot_new",
        "features": [{"feature_id": "BOSS_7", "kind": "hollow_boss", "parts": {"outer_side": [ref]}, "geometry_refs": [ref]}],
        "annotations": [{"annotation_id": "a1", "kind": "appearance", "geometry_refs": [ref]}],
        "relationships": [relation],
        "forming_scopes": [{"forming_scope_id": "SIDE_1", "kind": "undercut", "mode": "local"}],
        "coverage": [],
    }
    projected = provider.project_snapshot(input_record, state)
    assert projected.features[0].properties["parts"] == {"outer_side": [ref]}
    assert projected.features[0].relationships == [relation]
    assert projected.annotations == state["annotations"]
    assert projected.forming_scopes == state["forming_scopes"]
    with pytest.raises(DFMError, match="object array"):
        provider.project_snapshot(input_record, {**state, "annotations": {}})


def test_authoritative_revisions_preserve_reviews_only_when_the_geometry_is_unchanged():
    old = FeatureRecord("BOSS_7", "hollow_boss", [], 1.0, "a" * 64,
        properties={"geometry_refs": [{"kind": "face", "index": 1}], "human_note": "keep"}, status="confirmed")
    other = FeatureRecord("other", "main_wall", [], 1.0, "b" * 64)
    projected = replace(old, properties={"geometry_refs": [{"kind": "face", "index": 1}], "worker_snapshot_id": "new"},
        relationships=[{"from_ref": "BOSS_7", "to_ref": "SIDE_1", "type": "intersects_forming_scope"}])
    merged = reconcile_worker_features([old, other], [projected], "a" * 64)
    assert merged[0] == other
    assert merged[1].status == "confirmed"
    assert merged[1].properties["human_note"] == "keep"
    assert merged[1].relationships == projected.relationships
    changed = replace(projected, properties={"geometry_refs": [{"kind": "face", "index": 2}]})
    revised = reconcile_worker_features([old], [changed], "a" * 64)[0]
    assert revised.status == "candidate"
    assert revised.properties["geometry_refs"] == changed.properties["geometry_refs"]
    assert old.status == "confirmed"
