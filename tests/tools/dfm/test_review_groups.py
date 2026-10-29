from dataclasses import replace

from tools.dfm.contracts import FeatureRecord, FormingScopeRecord, GeometryRef
from tools.dfm.review import (
    annotation_review_groups,
    local_structure_review_content,
    local_structure_review_groups,
    structure_review_groups,
)


def test_attribute_review_is_content_bound_not_transport_or_source_bound():
    row = {
        "annotation_id": "a",
        "kind": "appearance",
        "value": "appearance",
        "geometry_refs": [{"kind": "face", "index": 1}],
        "properties": {},
        "source": "recognizer",
        "status": "active",
    }
    original = annotation_review_groups([row], [], snapshot_id="s1", created_at="now")
    confirmed = [replace(original[0], status="confirmed", confirmed_at="then")]
    same = annotation_review_groups(
        [{**row, "source": "user"}], confirmed, snapshot_id="s2", created_at="later"
    )
    assert same[0].status == "confirmed"
    changed = annotation_review_groups(
        [{**row, "value": "non_appearance"}], same, snapshot_id="s3", created_at="later"
    )
    assert [group.status for group in changed] == ["stale", "pending_review"]
    retired = annotation_review_groups(
        [{**row, "status": "stale"}], changed, snapshot_id="s4", created_at="later"
    )
    assert all(group.status == "stale" for group in retired)


def feature(identity, kind, face):
    return FeatureRecord(
        identity,
        kind,
        [],
        0.9,
        "a" * 64,
        properties={
            "geometry_refs": [{"kind": "face", "index": face}],
            "worker_snapshot_id": "old",
        },
    )


def test_confirmations_are_independent_and_only_changed_content_becomes_stale():
    wall, boss = feature("WALL", "main_wall", 1), feature("BOSS", "hollow_boss", 2)
    groups = structure_review_groups(
        [wall, boss], [], snapshot_id="discovery1", created_at="now"
    )
    assert len(groups) == 2
    assert {tuple(group.target_refs) for group in groups} == {("WALL",), ("BOSS",)}
    confirmed = [replace(group, status="confirmed") for group in groups]
    changed = replace(
        boss,
        properties={**boss.properties, "geometry_refs": [{"kind": "face", "index": 3}]},
    )
    revised = structure_review_groups(
        [wall, changed], confirmed, snapshot_id="discovery2", created_at="later"
    )
    assert (
        next(group for group in revised if group.target_refs == ["WALL"]).status
        == "confirmed"
    )
    assert {group.status for group in revised if group.target_refs == ["BOSS"]} == {
        "stale",
        "pending_review",
    }


def test_transport_and_local_membership_updates_do_not_revoke_structure_confirmation():
    boss = feature("BOSS", "hollow_boss", 2)
    group = structure_review_groups([boss], [], snapshot_id="d1", created_at="now")[0]
    updated = replace(
        boss,
        status="confirmed",
        source_refs=["new source"],
        properties={
            **boss.properties,
            "worker_snapshot_id": "new",
            "diagnostics": {"elapsed": 12},
        },
        relationships=[
            {
                "relation_id": "r",
                "type": "intersects_forming_scope",
                "from_ref": "BOSS",
                "to_ref": "SIDE1",
            }
        ],
    )
    groups = structure_review_groups(
        [updated],
        [replace(group, status="confirmed")],
        snapshot_id="d2",
        created_at="later",
    )
    assert len(groups) == 1
    assert groups[0].review_group_id == group.review_group_id
    assert groups[0].status == "confirmed"


def test_reference_pairing_changes_require_new_confirmation():
    boss = feature("BOSS", "hollow_boss", 2)
    group = structure_review_groups([boss], [], snapshot_id="d1", created_at="now")[0]
    changed = replace(
        boss,
        relationships=[
            {
                "relation_id": "r",
                "type": "adjacent_main_wall",
                "from_ref": "BOSS",
                "to_ref": "WALL2",
            }
        ],
    )
    groups = structure_review_groups(
        [changed],
        [replace(group, status="confirmed")],
        snapshot_id="d2",
        created_at="later",
    )
    assert [item.status for item in groups] == ["stale", "pending_review"]


def test_reference_audit_metadata_does_not_change_geometry_confirmation():
    relation = {
        "relation_id": "r",
        "type": "adjacent_main_wall",
        "from_ref": "BOSS",
        "to_ref": "WALL",
        "source": "recognizer",
    }
    boss = replace(feature("BOSS", "hollow_boss", 2), relationships=[relation])
    original = structure_review_groups([boss], [], snapshot_id="d1", created_at="now")[
        0
    ]
    updated = replace(
        boss,
        relationships=[{**relation, "source": "user_confirmed", "confidence": 1.0}],
    )
    groups = structure_review_groups(
        [updated],
        [replace(original, status="confirmed")],
        snapshot_id="d2",
        created_at="later",
    )
    assert len(groups) == 1
    assert groups[0].status == "confirmed"


def test_local_structure_review_excludes_direction_and_transport_but_tracks_membership():
    boss = feature("BOSS", "hollow_boss", 2)
    ref = GeometryRef("face", 2, "a" * 64, "topology", "face-2")
    scope = FormingScopeRecord(
        "SIDE", "s1", "local", None, "model", None, "candidate", [ref], kind="undercut"
    )
    relation = {
        "relation_id": "r",
        "type": "intersects_forming_scope",
        "from_ref": "BOSS",
        "to_ref": "SIDE",
        "geometry_refs": [ref.to_dict()],
    }
    original = local_structure_review_content(scope, [boss], [relation])
    updated = replace(
        scope,
        snapshot_id="s2",
        draw_direction=[1, 0, 0],
        direction_source="user_confirmed",
        status="confirmed",
    )
    assert (
        local_structure_review_content(
            updated, [replace(boss, status="confirmed")], [relation]
        )
        == original
    )
    assert local_structure_review_content(updated, [boss], []) != original


def test_returning_to_old_geometry_reopens_review_instead_of_restoring_old_approval():
    boss = feature("BOSS", "hollow_boss", 2)
    original = structure_review_groups([boss], [], snapshot_id="d1", created_at="now")[
        0
    ]
    changed = replace(
        boss,
        properties={**boss.properties, "geometry_refs": [{"kind": "face", "index": 3}]},
    )
    groups = structure_review_groups(
        [changed],
        [replace(original, status="confirmed")],
        snapshot_id="d2",
        created_at="later",
    )
    reverted = structure_review_groups(
        [boss], groups, snapshot_id="d3", created_at="latest"
    )
    active = [row for row in reverted if row.status != "stale"]
    assert len(active) == 1
    assert active[0].review_group_id == original.review_group_id
    assert active[0].status == "pending_review"
    assert active[0].confirmed_at is None
    assert active[0].snapshot_id == "d3"


def test_feature_removed_from_scope_leaves_a_confirmable_local_inventory_review():
    boss = feature("BOSS", "hollow_boss", 2)
    scope = FormingScopeRecord(
        "SIDE",
        "s1",
        "local",
        None,
        "model",
        None,
        "candidate",
        [GeometryRef("face", 2, "a" * 64, "topology", "face-2")],
        kind="undercut",
    )
    relation = {
        "relation_id": "r",
        "type": "contained_in_forming_scope",
        "from_ref": "BOSS",
        "to_ref": "SIDE",
    }
    groups = local_structure_review_groups(
        [scope],
        [boss],
        [relation],
        [],
        scope_ids={"SIDE"},
        snapshot_id="d1",
        created_at="now",
    )
    groups = [replace(row, status="confirmed") for row in groups]
    refreshed = local_structure_review_groups(
        [scope],
        [boss],
        [],
        groups,
        scope_ids={"SIDE"},
        snapshot_id="d2",
        created_at="later",
    )
    assert refreshed[0].status == "stale"
    assert refreshed[1].status == "pending_review"
    assert refreshed[1].target_refs == ["SIDE"]
