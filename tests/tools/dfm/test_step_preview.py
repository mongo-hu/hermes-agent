import json
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.analyzers.registry import AnalyzerRegistry
from tools.dfm.contracts import ArtifactRecord
from tools.dfm.errors import DFMError
from tools.dfm.service import DFMService


FIXTURE = Path("tests/fixtures/dfm/step/injection_plate_with_hole.step").resolve()


class PreviewAnalyzer:
    key = "occt_cpp"
    version = "test-preview"
    supported_inputs = ("step",)

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.contexts = []

    def capability(self, _context):
        raise AssertionError("preview delegates through run")

    def run(self, context, _cancellation):
        if self.fail:
            raise DFMError("geometry_engine_missing", "Preview engine unavailable.")
        self.contexts.append(context)
        output = context.project_dir / "runs" / context.run_id / "artifacts"
        output.mkdir(parents=True, exist_ok=True)
        payloads = {
            "render_scene": (
                "render_scene.json",
                {
                    "schema_version": 2,
                    "primitives": [],
                },
            ),
            "topology_map": (
                "topology_map.json",
                {"schema_version": 2, "faces": []},
            ),
        }
        artifacts = []
        for kind, (filename, payload) in payloads.items():
            path = output / filename
            path.write_text(json.dumps(payload), encoding="utf-8")
            artifacts.append(
                ArtifactRecord(
                    f"artifact_{kind}",
                    kind,
                    path.relative_to(context.project_dir).as_posix(),
                    "application/json",
                    "now",
                )
            )
        return artifacts


def _service(tmp_path, analyzer):
    token = set_hermes_home_override(tmp_path / "home")
    registry = AnalyzerRegistry()
    registry.register(analyzer)
    return token, DFMService(registry=registry, reconcile_jobs=False)


def test_step_registration_materializes_immediate_viewer_preview(tmp_path):
    analyzer = PreviewAnalyzer()
    token, service = _service(tmp_path, analyzer)
    try:
        project_id = service.project("create", name="Preview")["project_id"]

        result = service.project("add_input", project_id=project_id, path=str(FIXTURE))

        assert result["preview"]["status"] == "ready"
        manifest_path = Path(result["viewer_manifest"])
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "preview"
        assert manifest["issues"] == []
        assert [item.operation_id for item in analyzer.contexts[0].plan.operations] == [
            "geometry.preflight",
            "topology.index",
            "topology.aag",
        ]
    finally:
        service.close()
        reset_hermes_home_override(token)


def test_preview_failure_does_not_rollback_registered_input(tmp_path):
    token, service = _service(tmp_path, PreviewAnalyzer(fail=True))
    try:
        project_id = service.project("create", name="Preview fallback")["project_id"]

        result = service.project("add_input", project_id=project_id, path=str(FIXTURE))

        assert result["ok"] is True
        assert result["input"]["kind"] == "step"
        assert result["preview"]["status"] == "unavailable"
        assert result["preview"]["error"]["code"] == "geometry_engine_missing"
        assert "viewer_manifest" not in result
    finally:
        service.close()
        reset_hermes_home_override(token)
