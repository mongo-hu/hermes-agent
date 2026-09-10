from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.dfm.errors import DFMError
from tools.dfm.project.workspace import DFMWorkspace


@pytest.fixture
def hermes_home(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def test_workspace_is_profile_aware_and_creates_project_layout(hermes_home):
    workspace = DFMWorkspace()

    manifest = workspace.create_project("Bracket", idempotency_key="create-1")
    project_dir = workspace.project_dir(manifest.project_id)

    assert workspace.root == hermes_home / "workspace" / "dfm"
    assert {path.name for path in project_dir.iterdir()} == {
        "inputs",
        "runs",
        "artifacts",
        "reports",
        "project_manifest.json",
    }


def test_project_create_is_idempotent(hermes_home):
    workspace = DFMWorkspace()

    first = workspace.create_project("Bracket", idempotency_key="same-request")
    second = workspace.create_project("Different display name", idempotency_key="same-request")

    assert second.project_id == first.project_id
    assert second.name == "Bracket"


@pytest.mark.parametrize("project_id", ["../escape", "..", "a/b", "a\\b", ""])
def test_project_id_cannot_escape_workspace(hermes_home, project_id):
    workspace = DFMWorkspace()

    with pytest.raises(DFMError) as exc_info:
        workspace.project_dir(project_id)

    assert exc_info.value.code == "invalid_project_id"


def test_project_paths_are_resolved_below_workspace(hermes_home):
    workspace = DFMWorkspace()
    manifest = workspace.create_project("Bracket")

    assert workspace.project_dir(manifest.project_id).is_relative_to(Path(workspace.root))
