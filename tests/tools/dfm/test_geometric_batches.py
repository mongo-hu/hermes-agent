from dataclasses import replace

from tools.dfm.contracts import GeometricRequest, PlanFragmentRecord, PlanRecord
from tools.dfm.runtime.geometric_batches import next_geometric_batch


def bounded_batch(*args, **kwargs):
    return next_geometric_batch(*args, max_requests=8, **kwargs)


def fragment(
    identity,
    g,
    *,
    feature="BOSS",
    snapshot="s1",
    bindings=("ratio",),
    status="ready",
    context=None,
):
    return PlanFragmentRecord(
        identity,
        snapshot,
        "discovery",
        "worker:v2",
        "a" * 64,
        [],
        list(bindings),
        [GeometricRequest(feature, [g], context or {})],
        status=status,
    )


def plan(*fragments):
    return PlanRecord(
        "plan", "step", ["occt_cpp"], "ready", "now", plan_fragments=list(fragments)
    )


def test_first_batch_completes_shared_check_instead_of_sending_one_operand():
    own = fragment("own", "G_OWN", bindings=("own", "ratio"))
    ref = fragment("ref", "G_REF")
    other = fragment("other", "G_OTHER", feature="OTHER", bindings=("other",))
    batch = bounded_batch(plan(own, other, ref), set())
    assert batch.fragment_refs == {"own", "ref"}
    assert batch.items() == [
        {"feature_id": "BOSS", "geometric_ids": ["G_OWN", "G_REF"], "context": {}}
    ]


def test_unrelated_ready_check_is_preferred_over_an_operand_still_waiting_for_review():
    own = fragment("own", "G_OWN")
    ref = fragment("ref", "G_REF", status="pending_review")
    standalone = fragment("other", "G_OTHER", feature="OTHER", bindings=("other",))
    batch = bounded_batch(plan(own, ref, standalone), set())
    assert batch.fragment_refs == {"other"}


def test_later_batches_are_bounded_and_preserve_all_remaining_fragments():
    pending = [
        fragment(str(index), f"G_{index}", feature=f"FEATURE_{index}", bindings=())
        for index in range(19)
    ]
    value = plan(*pending)
    done = {"earlier"}
    sizes = []
    while batch := bounded_batch(value, done):
        sizes.append(sum(len(item["geometric_ids"]) for item in batch.items()))
        assert not batch.fragment_refs.intersection(done)
        done.update(batch.fragment_refs)
    assert sizes == [8, 8, 3]
    assert {row.fragment_id for row in pending} < done


def test_same_g_is_sent_once_but_keeps_every_consumer_fragment():
    a, b = fragment("a", "G_OWN"), fragment("b", "G_OWN", bindings=("other",))
    batch = bounded_batch(plan(a, b), set())
    assert len(batch.items()[0]["geometric_ids"]) == 1
    assert {
        owner.fragment_id for owners in batch.owners().values() for owner in owners
    } == {"a", "b"}


def test_contexts_and_snapshots_are_never_conflated():
    main = fragment("main", "G_DRAFT", context={"forming_scope_id": "MAIN"})
    side = fragment("side", "G_DRAFT", context={"forming_scope_id": "SIDE"})
    other = fragment("other", "G_DRAFT", snapshot="s2")
    batch = bounded_batch(plan(main, side, other), set())
    assert batch.fragment_refs == {"main", "side"}
    assert len(batch.items()) == 2
    assert (
        bounded_batch(plan(main, side, other), batch.fragment_refs).snapshot_id
        == "s2"
    )


def test_replaced_completed_unselected_and_different_build_fragments_are_excluded():
    old = fragment("old", "G_OWN")
    new = replace(fragment("new", "G_OWN"), supersedes="old")
    other = replace(fragment("other", "G_REF"), capability_version="other-build")
    value = plan(old, new, other)
    batch = bounded_batch(value, set(), selected={"new", "other"})
    assert batch.fragment_refs == {"new"}
    assert bounded_batch(value, {"new", "other"}) is None


def test_oversized_existing_fragment_runs_alone_not_partially_completed():
    large = replace(
        fragment("large", "G_FIRST"),
        items=[GeometricRequest("BOSS", [f"G_{index}" for index in range(10)])],
    )
    small = fragment("small", "G_LAST")
    batch = bounded_batch(plan(large, small), set())
    assert batch.fragment_refs == {"large"}
    assert len(batch.items()[0]["geometric_ids"]) == 10

def test_equivalent_direction_vectors_share_one_canonical_request_and_all_consumers():
    first = fragment("first", "G_DRAFT", context={"forming_scope_id": "SIDE", "draw_direction": [0, 0, 2]})
    second = fragment("second", "G_DRAFT", context={"forming_scope_id": "SIDE", "draw_direction": [0.0, 0.0, 1.0]})
    batch = bounded_batch(plan(first, second), set())
    assert len(batch.items()) == 1
    assert batch.items()[0]["context"]["draw_direction"] == [0.0, 0.0, 1.0]
    assert batch.fragment_refs == {"first", "second"}
    assert len(next(iter(batch.owners().values()))) == 2
    assert first.items[0].context["draw_direction"] == [0, 0, 2]


def test_confirmation_submits_all_ready_consumers_before_worker_executes():
    fragments = [fragment(str(i), "G_VALUE", feature=f"BOSS_{i}", bindings=()) for i in range(25)]
    batch = next_geometric_batch(plan(*fragments), set())
    assert batch.fragment_refs == {row.fragment_id for row in fragments}
    assert len(batch.items()) == 25
