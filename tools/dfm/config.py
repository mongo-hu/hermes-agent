"""Validated, profile-aware configuration for the built-in DFM capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import DFMError


GEOMETRY_BACKEND_OCCT_CPP_EXTERNAL = "occt_cpp_external"
GEOMETRY_BACKEND_PYTHONOCC_INTERNAL = "pythonocc_internal"

_GEOMETRY_BACKEND_ALIASES = {
    # Canonical user-facing names describe both implementation and deployment.
    GEOMETRY_BACKEND_OCCT_CPP_EXTERNAL: GEOMETRY_BACKEND_OCCT_CPP_EXTERNAL,
    GEOMETRY_BACKEND_PYTHONOCC_INTERNAL: GEOMETRY_BACKEND_PYTHONOCC_INTERNAL,
    # Legacy values remain readable so existing config and persisted callers do
    # not break during the naming migration.
    "occt_cpp": GEOMETRY_BACKEND_OCCT_CPP_EXTERNAL,
    "step": GEOMETRY_BACKEND_PYTHONOCC_INTERNAL,
}

_GEOMETRY_BACKEND_ANALYZERS = {
    GEOMETRY_BACKEND_OCCT_CPP_EXTERNAL: "occt_cpp",
    GEOMETRY_BACKEND_PYTHONOCC_INTERNAL: "step",
}


def normalize_geometry_backend(value: str) -> str:
    """Return the canonical user-facing backend identity when known."""

    normalized = value.strip()
    return _GEOMETRY_BACKEND_ALIASES.get(normalized, normalized)


def geometry_backend_analyzer_key(value: str) -> str:
    """Map a user-facing backend identity to the stable internal Analyzer key."""

    canonical = normalize_geometry_backend(value)
    return _GEOMETRY_BACKEND_ANALYZERS.get(canonical, canonical)


@dataclass(frozen=True)
class DFMConfig:
    runtime_python: str = "auto"
    default_process: str = "injection"
    max_concurrent_runs: int = 1
    timeout_seconds: int = 900
    max_file_size_mb: int = 200
    max_pages: int = 50
    keep_failed_runs: bool = True
    max_evidence_findings: int = 12
    nx_endpoint: str = ""
    nx_request_timeout_seconds: int = 30
    nx_poll_interval_seconds: int = 2
    ontology_endpoint: str = ""
    ontology_process: str = "injection"
    ontology_organization_id: str = ""
    ontology_sync_interval_seconds: int = 300
    ontology_request_timeout_seconds: int = 30
    ontology_pinned_snapshot_id: str = ""
    geometry_executable: str = ""
    geometry_timeout_seconds: int = 900
    drawing_enabled: bool = True
    geometry_backend: str = GEOMETRY_BACKEND_OCCT_CPP_EXTERNAL

    @property
    def geometry_analyzer_key(self) -> str:
        return geometry_backend_analyzer_key(self.geometry_backend)


def _nested(mapping: Mapping[str, Any], *keys: str, default: Any) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DFMError(
            "config_invalid", f"{path} must be a positive integer.", {"path": path}
        )
    return value


def load_dfm_config(config: Mapping[str, Any] | None = None) -> DFMConfig:
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    defaults = DFMConfig()
    runtime_python = _nested(
        config, "dfm", "runtime", "python", default=defaults.runtime_python
    )
    default_process = _nested(
        config, "dfm", "defaults", "process", default=defaults.default_process
    )
    keep_failed = _nested(
        config,
        "dfm",
        "retention",
        "keep_failed_runs",
        default=defaults.keep_failed_runs,
    )
    nx_endpoint = _nested(config, "dfm", "nx", "endpoint", default=defaults.nx_endpoint)
    geometry_executable = _nested(
        config, "dfm", "geometry", "executable", default=defaults.geometry_executable
    )
    drawing_enabled = _nested(
        config, "dfm", "drawing", "enabled", default=defaults.drawing_enabled
    )
    geometry_backend = _nested(
        config, "dfm", "geometry", "backend", default=defaults.geometry_backend
    )
    if not isinstance(nx_endpoint, str):
        raise DFMError("config_invalid", "dfm.nx.endpoint must be a string.")
    string_values = {
        "ontology_endpoint": _nested(
            config, "dfm", "ontology", "endpoint", default=defaults.ontology_endpoint
        ),
        "ontology_process": _nested(
            config, "dfm", "ontology", "process", default=defaults.ontology_process
        ),
        "ontology_organization_id": _nested(
            config,
            "dfm",
            "ontology",
            "organization_id",
            default=defaults.ontology_organization_id,
        ),
        "ontology_pinned_snapshot_id": _nested(
            config,
            "dfm",
            "ontology",
            "pinned_snapshot_id",
            default=defaults.ontology_pinned_snapshot_id,
        ),
    }
    if any(not isinstance(value, str) for value in string_values.values()):
        raise DFMError("config_invalid", "dfm.ontology string settings must be strings.")
    if not isinstance(geometry_executable, str):
        raise DFMError("config_invalid", "dfm.geometry.executable must be a string.")
    if not isinstance(drawing_enabled, bool):
        raise DFMError("config_invalid", "dfm.drawing.enabled must be boolean.")
    if not isinstance(geometry_backend, str) or not geometry_backend.strip():
        raise DFMError(
            "config_invalid", "dfm.geometry.backend must be a non-empty string."
        )
    if not isinstance(runtime_python, str) or not runtime_python.strip():
        raise DFMError(
            "config_invalid", "dfm.runtime.python must be a non-empty string."
        )
    if not isinstance(default_process, str) or not default_process.strip():
        raise DFMError(
            "config_invalid", "dfm.defaults.process must be a non-empty string."
        )
    normalized_process = default_process.strip()
    if normalized_process == "injection_molding":
        normalized_process = "injection"
    if not isinstance(keep_failed, bool):
        raise DFMError(
            "config_invalid", "dfm.retention.keep_failed_runs must be boolean."
        )
    return DFMConfig(
        runtime_python=runtime_python.strip(),
        default_process=normalized_process,
        max_concurrent_runs=_positive_int(
            _nested(
                config,
                "dfm",
                "runtime",
                "max_concurrent_runs",
                default=defaults.max_concurrent_runs,
            ),
            "dfm.runtime.max_concurrent_runs",
        ),
        timeout_seconds=_positive_int(
            _nested(
                config,
                "dfm",
                "runtime",
                "timeout_seconds",
                default=defaults.timeout_seconds,
            ),
            "dfm.runtime.timeout_seconds",
        ),
        max_file_size_mb=_positive_int(
            _nested(
                config,
                "dfm",
                "intake",
                "max_file_size_mb",
                default=defaults.max_file_size_mb,
            ),
            "dfm.intake.max_file_size_mb",
        ),
        max_pages=_positive_int(
            _nested(config, "dfm", "intake", "max_pages", default=defaults.max_pages),
            "dfm.intake.max_pages",
        ),
        keep_failed_runs=keep_failed,
        max_evidence_findings=_positive_int(
            _nested(
                config,
                "dfm",
                "evidence",
                "max_rendered_findings",
                default=defaults.max_evidence_findings,
            ),
            "dfm.evidence.max_rendered_findings",
        ),
        nx_endpoint=nx_endpoint.strip().rstrip("/"),
        nx_request_timeout_seconds=_positive_int(
            _nested(
                config,
                "dfm",
                "nx",
                "request_timeout_seconds",
                default=defaults.nx_request_timeout_seconds,
            ),
            "dfm.nx.request_timeout_seconds",
        ),
        nx_poll_interval_seconds=_positive_int(
            _nested(
                config,
                "dfm",
                "nx",
                "poll_interval_seconds",
                default=defaults.nx_poll_interval_seconds,
            ),
            "dfm.nx.poll_interval_seconds",
        ),
        ontology_endpoint=string_values["ontology_endpoint"].strip().rstrip("/"),
        ontology_process=string_values["ontology_process"].strip(),
        ontology_organization_id=string_values["ontology_organization_id"].strip(),
        ontology_sync_interval_seconds=_positive_int(
            _nested(
                config,
                "dfm",
                "ontology",
                "sync_interval_seconds",
                default=defaults.ontology_sync_interval_seconds,
            ),
            "dfm.ontology.sync_interval_seconds",
        ),
        ontology_request_timeout_seconds=_positive_int(
            _nested(
                config,
                "dfm",
                "ontology",
                "request_timeout_seconds",
                default=defaults.ontology_request_timeout_seconds,
            ),
            "dfm.ontology.request_timeout_seconds",
        ),
        ontology_pinned_snapshot_id=string_values["ontology_pinned_snapshot_id"].strip(),
        geometry_executable=geometry_executable.strip(),
        geometry_timeout_seconds=_positive_int(
            _nested(
                config,
                "dfm",
                "geometry",
                "timeout_seconds",
                default=defaults.geometry_timeout_seconds,
            ),
            "dfm.geometry.timeout_seconds",
        ),
        drawing_enabled=drawing_enabled,
        geometry_backend=normalize_geometry_backend(geometry_backend),
    )
