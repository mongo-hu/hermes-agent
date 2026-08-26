"""External OCCT C++ discovery boundary; implementation lives in another project."""

from __future__ import annotations

from typing import Any, Mapping

from .base import FeatureRecognitionResult
from ..contracts import DISCOVERY_SCHEMA_VERSION, InputRecord
from ..errors import DFMError


class OCCTCppFeatureRecognitionProvider:
    key = "occt_cpp_feature_recognition"
    version = "external-contract-1"

    def capability(self) -> dict:
        return {
            "provider": self.key,
            "version": self.version,
            "status": "not_implemented",
            "deployment": "external_project",
            "primary_production_target": True,
            "supported_formats": ["step"],
            "required_fact_names": ["process", "model_units"],
            "discovery_contract_version": DISCOVERY_SCHEMA_VERSION,
            "output_contracts": [
                "ObservationRecord[]",
                "FeatureRecord[]",
                "RegionRecord[]",
                "GeometryDiscoveryResultManifest",
            ],
        }

    def recognize(
        self,
        input_record: InputRecord,
        *,
        process: str,
        facts: Mapping[str, Any] | None = None,
    ) -> FeatureRecognitionResult:
        raise DFMError(
            "unsupported_capability",
            "The production OCCT C++ feature-recognition project is not connected yet.",
            self.capability(),
        )
