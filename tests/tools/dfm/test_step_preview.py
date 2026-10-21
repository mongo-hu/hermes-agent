import json
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.errors import DFMError
from tools.dfm.service import DFMService


FIXTURE = Path("tests/fixtures/dfm/step/injection_plate_with_hole.step").resolve()


class PreviewSessionPool:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def open_model(self, project_id, **kwargs):
        self.calls.append((project_id, kwargs))
        if self.fail:
            raise DFMError("geometry_session_lost", "Preview Worker unavailable.")
        artifact_dir = kwargs["artifact_dir"]
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifacts = {}
        for kind, payload in {
            "preflight": {"schema_version": 2},
            "topology_map": {"schema_version": 2, "faces": []},
            "render_scene": {"schema_version": 2, "primitives": []},
        }.items():
            path = artifact_dir / f"{kind}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            artifacts[kind] = str(path.resolve())
        return {"snapshot_id": "snapshot-preview", "artifacts": artifacts}

    def close(self):
        return None


def _service(tmp_path, pool):
    token = set_hermes_home_override(tmp_path / "home")
    service = DFMService(reconcile_jobs=False)
    service.geometry_sessions.close()
    service.geometry_sessions = pool
    return token, service


def test_step_registration_uses_the_project_owned_worker_preview(tmp_path):
    pool = PreviewSessionPool()
    token, service = _service(tmp_path, pool)
    try:
        project_id = service.project("create", name="Preview")["project_id"]

        result = service.project("add_input", project_id=project_id, path=str(FIXTURE))

        assert result["preview"]["status"] == "ready"
        assert result["preview"]["snapshot_id"] == "snapshot-preview"
        assert pool.calls[0][0] == project_id
        manifest_path = Path(result["viewer_manifest"])
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "preview"
        assert manifest["issues"] == []
    finally:
        service.close()
        reset_hermes_home_override(token)


def test_preview_failure_does_not_rollback_registered_input(tmp_path):
    token, service = _service(tmp_path, PreviewSessionPool(fail=True))
    try:
        project_id = service.project("create", name="Preview fallback")["project_id"]

        result = service.project("add_input", project_id=project_id, path=str(FIXTURE))

        assert result["ok"] is True
        assert result["input"]["kind"] == "step"
        assert result["preview"]["status"] == "unavailable"
        assert result["preview"]["error"]["code"] == "geometry_session_lost"
        assert "viewer_manifest" not in result
    finally:
        service.close()
        reset_hermes_home_override(token)
