"""Profile-aware filesystem layout for DFM projects."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from hermes_constants import get_hermes_home

from ..contracts import ProjectManifest
from ..errors import DFMError
from .manifest import ManifestStore


_PROJECT_ID = re.compile(r"^dfm_[a-f0-9]{12,32}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DFMWorkspace:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else get_hermes_home() / "workspace" / "dfm"
        self.projects_dir = self.root / "projects"

    def project_dir(self, project_id: str) -> Path:
        if not _PROJECT_ID.fullmatch(project_id or ""):
            raise DFMError(
                "invalid_project_id",
                "DFM project id is invalid.",
                {"project_id": project_id},
            )
        candidate = (self.projects_dir / project_id).resolve()
        if not candidate.is_relative_to(self.projects_dir.resolve()):
            raise DFMError("invalid_project_id", "DFM project path escapes its workspace.")
        return candidate

    def create_project(
        self,
        name: str,
        idempotency_key: str | None = None,
    ) -> ProjectManifest:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        if idempotency_key:
            for path in self.projects_dir.glob("dfm_*/project_manifest.json"):
                try:
                    existing = ManifestStore(path.parent).load()
                except DFMError:
                    continue
                if existing.idempotency_key == idempotency_key:
                    return existing

        project_id = f"dfm_{uuid4().hex[:16]}"
        project_dir = self.project_dir(project_id)
        project_dir.mkdir(parents=False, exist_ok=False)
        for child in ("inputs", "runs", "artifacts", "reports"):
            (project_dir / child).mkdir()
        now = _utc_now()
        manifest = ProjectManifest(
            project_id=project_id,
            name=(name or "Untitled DFM project").strip(),
            created_at=now,
            updated_at=now,
            idempotency_key=idempotency_key,
        )
        ManifestStore(project_dir).save(manifest)
        return manifest
