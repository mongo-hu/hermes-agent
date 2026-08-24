"""Backend-neutral capability contract for external geometry engines."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from ..contracts import DISCOVERY_SCHEMA_VERSION, OBJECTIVE_SCHEMA_VERSION


BACKEND_CAPABILITY_SCHEMA_VERSION = 1
_IMPLEMENTATION_STATUSES = {
    "certified",
    "experimental",
    "not_implemented",
    "unhealthy",
}


def _strings(value: dict[str, Any], key: str) -> tuple[str, ...]:
    items = value.get(key)
    if (
        not isinstance(items, list)
        or any(not isinstance(item, str) or not item for item in items)
        or len(items) != len(set(items))
    ):
        raise ValueError(f"Geometry backend capability {key} must be a string array.")
    return tuple(items)


def _validate_implementation(
    *,
    status: str,
    contract_version: int,
    expected_contract_version: int,
    implementation_version: str,
    certification_report_sha256: str,
    values: tuple[tuple[str, ...], ...],
) -> None:
    invalid_values = any(
        len(items) != len(set(items)) or any(not item for item in items)
        for items in values
    )
    if (
        status not in _IMPLEMENTATION_STATUSES
        or contract_version != expected_contract_version
        or invalid_values
        or (status in {"certified", "experimental"} and not implementation_version)
        or (
            certification_report_sha256
            and not re.fullmatch(r"[0-9a-f]{64}", certification_report_sha256)
        )
        or (status == "certified" and not certification_report_sha256)
    ):
        raise ValueError("Geometry implementation capability is invalid.")


@dataclass(frozen=True)
class GeometryRecognizerCapability:
    status: str
    contract_version: int = DISCOVERY_SCHEMA_VERSION
    implementation_version: str = ""
    required_fact_names: tuple[str, ...] = ()
    output_observation_kinds: tuple[str, ...] = ()
    output_feature_kinds: tuple[str, ...] = ()
    output_region_roles: tuple[str, ...] = ()
    supported_formats: tuple[str, ...] = ()
    certification_report_sha256: str = ""

    def __post_init__(self) -> None:
        _validate_implementation(
            status=self.status,
            contract_version=self.contract_version,
            expected_contract_version=DISCOVERY_SCHEMA_VERSION,
            implementation_version=self.implementation_version,
            certification_report_sha256=self.certification_report_sha256,
            values=(
                self.required_fact_names,
                self.output_observation_kinds,
                self.output_feature_kinds,
                self.output_region_roles,
                self.supported_formats,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "contract_version": self.contract_version,
            "implementation_version": self.implementation_version,
            "required_fact_names": list(self.required_fact_names),
            "output_observation_kinds": list(self.output_observation_kinds),
            "output_feature_kinds": list(self.output_feature_kinds),
            "output_region_roles": list(self.output_region_roles),
            "supported_formats": list(self.supported_formats),
            "certification_report_sha256": self.certification_report_sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryRecognizerCapability":
        return cls(
            status=str(payload.get("status") or "not_implemented"),
            contract_version=int(
                payload.get("contract_version") or DISCOVERY_SCHEMA_VERSION
            ),
            implementation_version=str(payload.get("implementation_version") or ""),
            required_fact_names=_strings(payload, "required_fact_names"),
            output_observation_kinds=_strings(
                payload, "output_observation_kinds"
            ),
            output_feature_kinds=_strings(payload, "output_feature_kinds"),
            output_region_roles=_strings(payload, "output_region_roles"),
            supported_formats=_strings(payload, "supported_formats"),
            certification_report_sha256=str(
                payload.get("certification_report_sha256") or ""
            ),
        )


@dataclass(frozen=True)
class GeometryCalculatorCapability:
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
    certification_report_sha256: str = ""

    def __post_init__(self) -> None:
        _validate_implementation(
            status=self.status,
            contract_version=self.contract_version,
            expected_contract_version=OBJECTIVE_SCHEMA_VERSION,
            implementation_version=self.implementation_version,
            certification_report_sha256=self.certification_report_sha256,
            values=(
                self.required_arguments,
                self.optional_arguments,
                self.supported_algorithm_options,
                self.output_quantities,
                self.output_artifact_kinds,
                self.supported_formats,
                self.supported_region_modes,
            ),
        )

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
            "certification_report_sha256": self.certification_report_sha256,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryCalculatorCapability":
        return cls(
            status=str(payload.get("status") or "not_implemented"),
            contract_version=int(
                payload.get("contract_version") or OBJECTIVE_SCHEMA_VERSION
            ),
            implementation_version=str(payload.get("implementation_version") or ""),
            required_arguments=_strings(payload, "required_arguments"),
            optional_arguments=_strings(payload, "optional_arguments"),
            supported_algorithm_options=_strings(
                payload, "supported_algorithm_options"
            ),
            output_quantities=_strings(payload, "output_quantities"),
            output_artifact_kinds=_strings(payload, "output_artifact_kinds"),
            supported_formats=_strings(payload, "supported_formats"),
            supported_region_modes=_strings(payload, "supported_region_modes"),
            certification_report_sha256=str(
                payload.get("certification_report_sha256") or ""
            ),
        )


@dataclass(frozen=True)
class GeometryBackendCapability:
    schema_version: int
    status: str
    backend_id: str
    backend_version: str
    formats: dict[str, str] = field(default_factory=dict)
    recognizers: dict[str, GeometryRecognizerCapability] = field(default_factory=dict)
    calculators: dict[str, GeometryCalculatorCapability] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.schema_version != BACKEND_CAPABILITY_SCHEMA_VERSION
            or self.status not in {"available", "unhealthy"}
            or not self.backend_id
            or not self.backend_version
            or any(not key for key in self.formats)
            or any(value not in _IMPLEMENTATION_STATUSES for value in self.formats.values())
            or any(not key for key in self.recognizers)
            or any(not key for key in self.calculators)
        ):
            raise ValueError("Geometry backend capability identity is invalid.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "formats": dict(self.formats),
            "recognizers": {
                key: value.to_dict() for key, value in self.recognizers.items()
            },
            "calculators": {
                key: value.to_dict() for key, value in self.calculators.items()
            },
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeometryBackendCapability":
        return cls(
            schema_version=int(payload.get("schema_version") or 0),
            status=str(payload.get("status") or "unhealthy"),
            backend_id=str(payload.get("backend_id") or ""),
            backend_version=str(payload.get("backend_version") or ""),
            formats={
                str(key): str(value)
                for key, value in dict(payload.get("formats") or {}).items()
            },
            recognizers={
                str(key): GeometryRecognizerCapability.from_dict(value)
                for key, value in dict(payload.get("recognizers") or {}).items()
            },
            calculators={
                str(key): GeometryCalculatorCapability.from_dict(value)
                for key, value in dict(payload.get("calculators") or {}).items()
            },
            details=dict(payload.get("details") or {}),
        )
