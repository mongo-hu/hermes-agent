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
            "status": "available_via_occt_analyzer",
            "deployment": "external_process",
            "supported_formats": ["step"],
            "required_fact_names": ["process", "model_units"],
            "discovery_contract_version": DISCOVERY_SCHEMA_VERSION,
            "pythonocc": "deprecated_disabled",
            "output_contracts": [
                "FeatureRecord[]",
                "RegionRecord[]",
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
            "external_process_required",
            "OCCT feature recognition must run through the isolated OCCT analyzer; "
            "the provider contract does not execute native code in-process.",
            {
                **self.capability(),
                "input_id": input_record.input_id,
                "process": process,
            },
        )

