from dataclasses import replace

import pytest

from tools.dfm.contracts import (
    FeatureRecord,
    GeometricFindingRecord,
    GeometricRequest,
    PlanFragmentRecord,
    PlanRecord,
    ProjectManifest,
)
from tools.dfm.reference_dependencies import resolve_reference_dependencies
from tools.dfm.review import structure_review_groups
from tools.dfm.service import DFMService


REQUIREMENT = [{"type": "adjacent_main_wall", "target_feature_kind": "main_wall"}]


@pytest.fixture
def graph():
    ref = {"kind": "face", "index": 201}
    wall = FeatureRecord(
        "WALL", "main_wall", [], 1.0, "a" * 64, properties={"geometry_refs": [ref]}
    )
    boss = FeatureRecord(
        "BOSS",
        "hollow_boss",
        [],
        1.0,
        "a" * 64,
        properties={"geometry_refs": [{"kind": "face", "index": 101}]},
    )
    group = structure_review_groups([wall], [], snapshot_id="d1", created_at="now")[0]
    relation = {
        "relation_id": "reference",
        "type": "adjacent_main_wall",
        "from_ref": "BOSS",
        "to_ref": "WALL",
        "geometry_refs": [ref],
    }
    return boss, wall, group, relation


def resolve(
    graph, requirements=REQUIREMENT, *, relations=None, group_status="confirmed"
):
    boss, wall, group, relation = graph
    return resolve_reference_dependencies(
        boss,
        "G_REFERENCE",
        requirements,
        features={wall.feature_id: wall, boss.feature_id: boss},
        relationships={
            ("BOSS", "adjacent_main_wall"): [relation]
            if relations is None
            else relations
        },
        reviews={wall.feature_id: [replace(group, status=group_status)]},
    )


def test_g_without_reference_dependency_does_not_wait_for_another_feature(graph):
    assert resolve(graph, [], group_status="pending_review") == ([], [], [])


def test_reference_target_review_is_required_without_requiring_local_membership(graph):
    dependencies, groups, blockers = resolve(graph, group_status="pending_review")
    assert dependencies[0]["target_feature_id"] == "WALL"
    assert dependencies[0]["geometry_refs"] == graph[3]["geometry_refs"]
    assert dependencies[0]["review_group_refs"] == [groups[0].review_group_id]
    assert blockers[0]["code"] == "pending_review"
    assert blockers[0]["target_feature_id"] == "WALL"
    assert resolve(graph)[2] == []


@pytest.mark.parametrize(
    "count, code",
    [(0, "missing_reference_relationship"), (2, "ambiguous_reference_relationship")],
)
def test_missing_and_ambiguous_references_do_not_select_a_fallback(graph, count, code):
    relations = [{**graph[3], "relation_id": f"r{index}"} for index in range(count)]
    dependencies, groups, blockers = resolve(graph, relations=relations)
    assert dependencies == groups == []
    assert blockers[0]["code"] == code


@pytest.mark.parametrize(
    "change",
    [
        {"to_ref": "BOSS"},
        {"geometry_refs": []},
        {"geometry_refs": [{"kind": "face", "index": 202}]},
        {"patch_mask_ref": "not_supported"},
    ],
)
def test_invalid_reference_inputs_cannot_become_ready(graph, change):
    dependencies, groups, blockers = resolve(graph, relations=[{**graph[3], **change}])
    assert dependencies == groups == []
    assert blockers[0]["code"] == "invalid_reference_relationship"


def test_reference_change_invalidates_dependent_findings_but_not_source_only_results(
    graph,
):
    dependencies, groups, blockers = resolve(graph)
    reference = PlanFragmentRecord(
        "ref",
        "s1",
        "d1",
        "caps",
        "a" * 64,
        [],
        [],
        [GeometricRequest("BOSS", ["G_REFERENCE"], {})],
        reference_dependencies=dependencies,
    )
    own = replace(
        reference,
        fragment_id="own",
        items=[GeometricRequest("BOSS", ["G_OWN"], {})],
        reference_dependencies=[],
    )
    plans = [
        PlanRecord(
            "ref_plan", "step", ["occt_cpp"], "ready", "now", plan_fragments=[reference]
        ),
        PlanRecord(
            "own_plan", "step", ["occt_cpp"], "ready", "now", plan_fragments=[own]
        ),
    ]

    def finding(identity, geometric_id):
        return GeometricFindingRecord(
            identity,
            "fixture",
            "unclassified",
            "open",
            [],
            [],
            [geometric_id],
            [],
            [],
            "fixture",
            feature_refs=["BOSS"],
        )

    manifest = ProjectManifest(
        "dfm_test",
        "references",
        "now",
        "now",
        plans=plans,
        findings=[
            finding("reference_issue", "G_REFERENCE"),
            finding("own_issue", "G_OWN"),
        ],
    )
    revised = DFMService._invalidate_plans_for_semantics(manifest, "feature:WALL")
    assert [plan.status for plan in revised] == ["invalidated", "ready"]
    findings = DFMService._invalidate_findings_for_semantics(manifest, "feature:WALL")
    assert [item.status for item in findings] == ["stale", "open"]
    assert (
        PlanFragmentRecord.from_dict(reference.to_dict()).reference_dependencies
        == dependencies
    )


def test_old_reference_plan_does_not_invalidate_a_finding_bound_to_another_wall(graph):
    dependencies, _, _ = resolve(graph)
    original = PlanFragmentRecord(
        "old",
        "s1",
        "d1",
        "caps",
        "a" * 64,
        [],
        [],
        [GeometricRequest("BOSS", ["G_REFERENCE"], {})],
        reference_dependencies=dependencies,
    )
    current = replace(
        original,
        fragment_id="current",
        supersedes="old",
        reference_dependencies=[{**dependencies[0], "target_feature_id": "OTHER_WALL"}],
    )
    plan = PlanRecord(
        "plan", "step", ["occt_cpp"], "ready", "now", plan_fragments=[original, current]
    )
    finding = GeometricFindingRecord(
        "current_issue",
        "fixture",
        "unclassified",
        "open",
        [],
        [],
        ["G_REFERENCE"],
        [],
        [],
        "fixture",
        feature_refs=["BOSS"],
        plan_fragment_refs=["current"],
    )
    manifest = ProjectManifest(
        "dfm_test", "references", "now", "now", plans=[plan], findings=[finding]
    )
    assert (
        DFMService._invalidate_findings_for_semantics(manifest, "feature:WALL")[
            0
        ].status
        == "open"
    )
    assert (
        DFMService._invalidate_findings_for_semantics(manifest, "feature:OTHER_WALL")[
            0
        ].status
        == "stale"
    )
