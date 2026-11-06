from copy import deepcopy

import pytest

from tools.dfm.discovery import _normalize_coverage
from tools.dfm.errors import DFMError


def row(scope_id, context=None):
    return {
        "feature_kind": "forming_plan",
        "forming_scope_id": scope_id,
        "context": context or {},
        "status": "partial",
        "diagnostics": {"requires_user_input": True},
    }


def test_different_forming_scopes_keep_their_coverage_and_identity():
    raw = [
        row("undercut_A"),
        row("undercut_B"),
        row("MAIN", {"draw_direction": [0, 0, 1]}),
    ]
    before = deepcopy(raw)
    normalized = _normalize_coverage(raw)
    assert [item["forming_scope_id"] for item in normalized] == [
        "undercut_A",
        "undercut_B",
        "MAIN",
    ]
    assert all(item["status"] == "partial" for item in normalized)
    assert all(item["diagnostics"]["requires_user_input"] for item in normalized)
    assert _normalize_coverage(normalized) == normalized
    assert raw == before


def test_same_scope_and_context_is_still_rejected_as_duplicate():
    with pytest.raises(DFMError) as error:
        _normalize_coverage([
            row("SIDE", {"a": 1, "b": 2}),
            row("SIDE", {"b": 2, "a": 1}),
        ])
    assert error.value.code == "geometry_protocol_invalid"
    assert error.value.details["forming_scope_id"] == "SIDE"


def test_coverage_context_and_scope_both_participate_in_identity():
    normalized = _normalize_coverage([
        row("SIDE", {"draw_direction": [1, 0, 0]}),
        row("SIDE", {"draw_direction": [0, 0, 1]}),
        {"feature_kind": "main_wall", "context": {}, "status": "complete"},
    ])
    assert len(normalized) == 3
    assert "forming_scope_id" not in normalized[2]


def test_missing_and_null_scope_have_the_same_native_identity():
    unscoped = {"feature_kind": "main_wall", "context": {}, "status": "complete"}
    with pytest.raises(DFMError):
        _normalize_coverage([unscoped, {**unscoped, "forming_scope_id": None}])


@pytest.mark.parametrize("scope_id", [False, 12, [], {}, ""])
def test_invalid_forming_scope_identity_is_rejected(scope_id):
    with pytest.raises(DFMError) as error:
        _normalize_coverage([row(scope_id)])
    assert error.value.code == "geometry_protocol_invalid"
