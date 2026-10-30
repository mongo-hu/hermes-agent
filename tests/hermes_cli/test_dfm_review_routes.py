"""Desktop HTTP requests use the same DFM confirmation entry points as the Agent."""

from unittest.mock import Mock

import pytest

from tools.dfm.errors import DFMError


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient
    import hermes_cli.web_server as server
    import tools.dfm.service as service_module

    state = {
        "ok": True,
        "project_id": "dfm_test",
        "revision": 7,
        "features": [],
        "review_groups": [
            {"kind": "undercut_scope", "status": "confirmed", "target_refs": ["SIDE"]},
            {"kind": "local_structure", "status": "confirmed", "target_refs": ["SIDE"]},
        ],
        "forming_scopes": [
            {
                "forming_scope_id": "SIDE",
                "kind": "undercut",
                "mode": "local",
                "geometry_refs": [{"kind": "face", "index": 12}],
            }
        ],
    }
    service = Mock()
    service.analysis.side_effect = lambda action, **params: state
    monkeypatch.setattr(service_module, "get_dfm_service", lambda: service)
    client = TestClient(
        server.app,
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {server._SESSION_TOKEN}"},
    )
    return client, service


def test_review_context_is_read_only_and_confirmation_keeps_revision_and_group_ids(api):
    client, service = api
    assert client.get("/api/dfm/projects/dfm_test/discovery/review").status_code == 200
    service.analysis.assert_called_once_with("review_context", project_id="dfm_test")
    result = client.post(
        "/api/dfm/projects/dfm_test/discovery/review/confirm",
        json={"review_group_ids": ["group1"], "expected_revision": 7},
    )
    assert result.status_code == 200, result.text
    assert service.analysis.call_args_list[-2].kwargs == {
        "project_id": "dfm_test",
        "review_group_ids": ["group1"],
        "expected_revision": 7,
    }


def test_direction_uses_current_server_geometry_and_user_supplied_vector(api):
    client, service = api
    result = client.post(
        "/api/dfm/projects/dfm_test/discovery/review/direction",
        json={
            "forming_scope_id": "SIDE",
            "draw_direction": [1, 2, 0],
            "expected_revision": 7,
        },
    )
    assert result.status_code == 200, result.text
    scope = service.analysis.call_args_list[-2].kwargs["forming_scopes"][0]
    assert scope["geometry_refs"] == [{"kind": "face", "index": 12}]
    assert scope["kind"] == "undercut"
    assert scope["draw_direction"] == [1, 2, 0]
    assert scope["direction_source"] == "user_confirmed"


def test_unreviewed_undercut_direction_is_staged_instead_of_confirmed(api):
    client, service = api
    state = service.analysis("review_context")
    state["review_groups"] = []
    service.reset_mock()
    result = client.post("/api/dfm/projects/dfm_test/discovery/review/direction", json={
        "forming_scope_id": "SIDE", "draw_direction": [1, 0, 0], "expected_revision": 7})
    assert result.status_code == 200, result.text
    call = service.analysis.call_args_list[-2]
    assert call.args == ("stage_forming_direction",)
    assert call.kwargs["forming_scope"]["draw_direction"] == [1, 0, 0]


def test_edit_prepare_keeps_review_snapshot_pinned(api):
    client, service = api
    result = client.post("/api/dfm/projects/dfm_test/discovery/review/prepare", json={
        "expected_revision": 7, "expected_snapshot_id": "snapshot-reviewed"})
    assert result.status_code == 200, result.text
    service.analysis.assert_called_once_with("prepare_review", project_id="dfm_test",
        expected_revision=7, expected_snapshot_id="snapshot-reviewed")


def test_attribute_edit_preserves_identity_geometry_and_revision(api):
    client, service = api
    rows = [
        {
            "annotation_id": "a",
            "kind": "appearance",
            "value": "appearance",
            "geometry_refs": [{"kind": "face", "index": 12}],
        }
    ]
    result = client.patch(
        "/api/dfm/projects/dfm_test/discovery/annotations",
        json={
            "annotations": rows,
            "retire_annotation_ids": ["old"],
            "expected_revision": 7,
        },
    )
    assert result.status_code == 200, result.text
    service.analysis.assert_called_once_with(
        "revise_annotations",
        project_id="dfm_test",
        annotations=rows,
        retire_annotation_ids=["old"],
        expected_revision=7,
    )
    service.analysis.side_effect = DFMError("geometry_edit_conflict", "stale")
    result = client.patch(
        "/api/dfm/projects/dfm_test/discovery/annotations",
        json={"annotations": rows, "expected_revision": 7},
    )
    assert result.status_code == 409


def test_empty_selection_does_not_implicitly_confirm_everything(api):
    client, service = api
    result = client.post(
        "/api/dfm/projects/dfm_test/discovery/review/confirm",
        json={"review_group_ids": [], "expected_revision": 7},
    )
    assert result.status_code == 400
    service.analysis.assert_not_called()


def test_review_snapshot_condition_is_forwarded_to_all_mutation_routes(api):
    client, service = api
    requests = [
        ("POST", "review/confirm", {"review_group_ids": ["group1"]}),
        (
            "POST",
            "review/direction",
            {"forming_scope_id": "SIDE", "draw_direction": [1, 0, 0]},
        ),
        ("PATCH", "annotations", {"annotations": [{"annotation_id": "a"}]}),
        ("PATCH", "features/BOSS", {"geometry_refs": [{"kind": "face", "index": 12}]}),
        ("PATCH", "scopes/SIDE", {"geometry_refs": [{"kind": "face", "index": 12}]}),
    ]
    for method, suffix, fields in requests:
        service.analysis.reset_mock()
        response = client.request(
            method,
            f"/api/dfm/projects/dfm_test/discovery/{suffix}",
            json={
                **fields,
                "expected_revision": 7,
                "expected_snapshot_id": "discovery.snapshot.test",
            },
        )
        assert response.status_code == 200, response.text
        assert any(
            call.kwargs.get("expected_snapshot_id") == "discovery.snapshot.test"
            for call in service.analysis.call_args_list
        )


def test_conflict_is_reported_as_conflict_and_geometry_refs_are_not_dropped(api):
    client, service = api
    service.analysis.side_effect = DFMError("manifest_conflict", "changed")
    result = client.post(
        "/api/dfm/projects/dfm_test/discovery/review/confirm",
        json={"review_group_ids": ["group1"], "expected_revision": 7},
    )
    assert result.status_code == 409
    service.analysis.side_effect = lambda action, **params: {
        "revision": 8,
        "geometry_refs": params["geometry_refs"],
    }
    refs = [
        {
            "kind": "face",
            "index": 12,
            "input_sha256": "a" * 64,
            "topology_snapshot_id": "topo",
            "entity_id": "face12",
        }
    ]
    result = client.patch(
        "/api/dfm/projects/dfm_test/discovery/features/feature1",
        json={"geometry_refs": refs, "expected_revision": 7},
    )
    assert result.status_code == 200, result.text
    assert service.analysis.call_args.kwargs["geometry_refs"] == refs
    assert "face_indices" not in service.analysis.call_args.kwargs


def test_scope_geometry_edit_preserves_full_refs_and_returns_updated_review_context(
    api,
):
    client, service = api
    state = service.analysis("review_context")
    service.reset_mock()
    service.analysis.side_effect = lambda action, **params: (
        {"revision": 8, "viewer_manifest": "/project/current-viewer.json", "unchanged": False}
        if action == "update_forming_scope_geometry" else {**state, "revision": 8}
    )
    refs = [
        {
            "kind": "face",
            "index": 12,
            "input_sha256": "a" * 64,
            "topology_snapshot_id": "topo",
            "entity_id": "face12",
        }
    ]
    result = client.patch(
        "/api/dfm/projects/dfm_test/discovery/scopes/SIDE",
        json={"geometry_refs": refs, "expected_revision": 7},
    )
    assert result.status_code == 200, result.text
    assert result.json()["viewer_manifest"] == "/project/current-viewer.json"
    assert result.json()["revision"] == 8
    assert result.json()["unchanged"] is False
    call = service.analysis.call_args_list[-2]
    assert call.args == ("update_forming_scope_geometry",)
    assert call.kwargs == {
        "project_id": "dfm_test",
        "forming_scope_id": "SIDE",
        "geometry_refs": refs,
        "expected_revision": 7,
    }
    service.analysis.side_effect = lambda action, **params: {**state, "unchanged": True}
    unchanged = client.patch("/api/dfm/projects/dfm_test/discovery/scopes/SIDE",
        json={"geometry_refs": refs, "expected_revision": 7})
    assert unchanged.json()["unchanged"] is True
    service.analysis.side_effect = DFMError(
        "geometry_edit_conflict", "another edit committed"
    )
    assert (
        client.patch(
            "/api/dfm/projects/dfm_test/discovery/scopes/SIDE",
            json={"geometry_refs": refs, "expected_revision": 7},
        ).status_code
        == 409
    )
