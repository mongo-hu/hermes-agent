"""Versioned, JSON-only contracts for the external NX compute service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...contracts import OBJECTIVE_SCHEMA_VERSION


NX_API_VERSION = "v1"
NX_REQUEST_SCHEMA_VERSION = OBJECTIVE_SCHEMA_VERSION


@dataclass(frozen=True)
class NXCalculatorCapability:
    status: str
    contract_version: int = OBJECTIVE_SCHEMA_VERSION
    implementation_version: str = ""
    required_arguments: tuple[str, ...] = ()
    optional_arguments: tuple[str, ...] = ()
    supported_algorithm_options: tuple[str, ...] = ()
    output_quantities: tuple[str, ...] = ()
    output_artifact_kinds: tuple[str, ...] = ()
    supported_formats: tuple[str, ...] = ()
    supported_region_modes: tuple[str, ...] = ()
    supported_nx_versions: tuple[str, ...] = ()
    certification_report_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "contract_version": self.contract_version,
            "implementation_version": self.implementation_version,
            "required_arguments": list(self.required_arguments),
            "optional_arguments": list(self.optional_arguments),
            "supported_algorithm_options": list(self.supported_algorithm_options),
            "output_quantities": list(self.output_quantities),
            "output_artifact_kinds": list(self.output_artifact_kinds),
            "supported_formats": list(self.supported_formats),
            "supported_region_modes": list(self.supported_region_modes),
            "supported_nx_versions": list(self.supported_nx_versions),
            "certification_report_sha256": self.certification_report_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NXCalculatorCapability":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("Calculator capability must be a JSON object.")
        required = {
            "status",
            "contract_version",
            "implementation_version",
            "required_arguments",
            "optional_arguments",
            "supported_algorithm_options",
            "output_quantities",
            "output_artifact_kinds",
            "supported_formats",
            "supported_region_modes",
            "supported_nx_versions",
            "certification_report_sha256",
        }
        if set(value) != required:
            raise ValueError(
                "Calculator capability fields do not match the objective schema."
            )
        try:
            contract_version = int(
                value.get("contract_version") or OBJECTIVE_SCHEMA_VERSION
            )
        except (TypeError, ValueError):
            contract_version = 0

        def strings(key: str) -> tuple[str, ...]:
            items = value.get(key)
            if not isinstance(items, list):
                raise ValueError(f"Calculator capability {key} must be an array.")
            return tuple(str(item) for item in items)

        return cls(
            status=str(value.get("status") or "not_implemented"),
            contract_version=contract_version,
            implementation_version=str(value.get("implementation_version") or ""),
            required_arguments=strings("required_arguments"),
            optional_arguments=strings("optional_arguments"),
            supported_algorithm_options=strings("supported_algorithm_options"),
            output_quantities=strings("output_quantities"),
            output_artifact_kinds=strings("output_artifact_kinds"),
            supported_formats=strings("supported_formats"),
            supported_region_modes=strings("supported_region_modes"),
            supported_nx_versions=strings("supported_nx_versions"),
            certification_report_sha256=str(
                value.get("certification_report_sha256") or ""
            ),
        )


@dataclass(frozen=True)
class NXCapability:
    status: str
    backend_version: str = ""
    plugin_version: str = ""
    formats: dict[str, str] = field(default_factory=dict)
    calculators: dict[str, NXCalculatorCapability] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "backend_version": self.backend_version,
            "plugin_version": self.plugin_version,
            "formats": dict(self.formats),
            "calculators": {
                key: value.to_dict() for key, value in self.calculators.items()
            },
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NXCapability":
        return cls(
            status=str(payload.get("status") or "unhealthy"),
            backend_version=str(payload.get("backend_version") or ""),
            plugin_version=str(payload.get("plugin_version") or ""),
            formats=dict(payload.get("formats") or {}),
            calculators={
                str(key): NXCalculatorCapability.from_dict(value)
                for key, value in dict(payload.get("calculators") or {}).items()
            },
            details=dict(payload.get("details") or {}),
        )

    def calculator(self, calculator_id: str) -> NXCalculatorCapability:
        return self.calculators.get(
            calculator_id, NXCalculatorCapability(status="not_implemented")
        )


@dataclass(frozen=True)
class NXJobStatus:
    job_id: str
    status: str
    stage: str = ""
    progress_percent: int = 0
    error: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NXJobStatus":
        return cls(
            job_id=str(payload.get("job_id") or ""),
            status=str(payload.get("status") or ""),
            stage=str(payload.get("stage") or ""),
            progress_percent=int(payload.get("progress_percent") or 0),
            error=dict(payload["error"])
            if isinstance(payload.get("error"), dict)
            else None,
        )
