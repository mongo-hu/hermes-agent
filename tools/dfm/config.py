"""Validated, profile-aware configuration for the built-in DFM capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import DFMError


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
    geometry_executable: str = ""
    geometry_timeout_seconds: int = 900
    drawing_enabled: bool = True
    geometry_backend: str = "step"


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
        geometry_backend=geometry_backend.strip(),
    )
