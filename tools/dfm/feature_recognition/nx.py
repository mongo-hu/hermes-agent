"""NX semantic feature-recognition boundary; implementation intentionally pending."""

from __future__ import annotations

from typing import Any, Mapping

from .base import FeatureRecognitionResult
from ..contracts import InputRecord
from ..errors import DFMError


class NXFeatureRecognitionProvider:
    key = "nx_feature_recognition"
    version = "placeholder-1"

    def capability(self) -> dict:
        return {
            "provider": self.key,
            "version": self.version,
            "status": "not_implemented",
            "supported_formats": ["step", "parasolid_xt"],
            "required_fact_names": ["process", "model_units", "pull_dir"],
            "output_contracts": ["FeatureRecord[]", "RegionRecord[]"],
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
            "NX feature recognition is a declared placeholder and cannot execute yet.",
            self.capability(),
        )
