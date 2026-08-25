"""OCCT C++ adapter behind the stable Hermes feature-recognition contract."""

from __future__ import annotations

from typing import Any, Mapping

from .base import FeatureRecognitionResult
from ..contracts import DISCOVERY_SCHEMA_VERSION, InputRecord
from ..errors import DFMError


class OCCTCppFeatureRecognitionProvider:
    key = "occt_cpp_feature_recognition"
    version = "dfm.geometry.request/v1"

    def capability(self) -> dict[str, Any]:
        return {
            "provider": self.key,
            "version": self.version,
            "status": "not_implemented",
            "deployment": "external_project",
            "primary_production_target": True,
            "supported_formats": ["step"],
            "required_fact_names": ["process", "model_units"],
            "discovery_contract_version": DISCOVERY_SCHEMA_VERSION,
            "pythonocc": "deprecated_disabled",
            "output_contracts": [
                "FeatureRecord[]",
                "RegionRecord[]",
                "GeometryDiscoveryResultManifest",
                "DiscoverySnapshotRecord",
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
            "The external OCCT Geometry Discovery/Recognizer call is not connected; "
            "the Objective analyzer output must not be relabeled as a Discovery result.",
            {
                **self.capability(),
                "input_id": input_record.input_id,
                "process": process,
            },
        )

