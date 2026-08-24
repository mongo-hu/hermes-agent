"""Feature-recognition provider boundaries used by DFM Discovery."""

from .base import FeatureRecognitionProvider, FeatureRecognitionResult
from .occt_cpp import OCCTCppFeatureRecognitionProvider

__all__ = [
    "FeatureRecognitionProvider",
    "FeatureRecognitionResult",
    "OCCTCppFeatureRecognitionProvider",
]

