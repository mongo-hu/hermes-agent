"""Analyzer for Observation/Feature fusion running 2D and 3D in parallel."""

import concurrent.futures
from ..contracts import ArtifactRecord, Capability, CapabilityStatus
from ..errors import DFMError
from .base import AnalyzerContext, CancellationToken


class FusionAnalyzer:
    key = "fusion"
    version = "1.0.0"
    supported_inputs = ("fusion",)

    def capability(self, context: AnalyzerContext) -> Capability:
        return Capability(
            self.key,
            CapabilityStatus.AVAILABLE,
            "Fusion analyzer is ready.",
        )

    def run(self, context: AnalyzerContext, cancellation: CancellationToken) -> list[ArtifactRecord]:
        cancellation.raise_if_cancelled()
        capability = self.capability(context)
        if capability.status != CapabilityStatus.AVAILABLE:
            raise DFMError("unsupported_capability", capability.reason, capability.details)

        from .step import StepAnalyzer
        from .drawing import DrawingAnalyzer

        step_analyzer = StepAnalyzer()
        drawing_analyzer = DrawingAnalyzer()

        step_cap = step_analyzer.capability(context)
        drawing_cap = drawing_analyzer.capability(context)

        if step_cap.status != CapabilityStatus.AVAILABLE:
            raise DFMError("fusion_failed", "3D Step analyzer is not available.", step_cap.details)
        if drawing_cap.status != CapabilityStatus.AVAILABLE:
            raise DFMError("fusion_failed", "2D Drawing analyzer is not available.", drawing_cap.details)

        all_artifacts = []

        # Run them in parallel using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_step = executor.submit(step_analyzer.run, context, cancellation)
            future_drawing = executor.submit(drawing_analyzer.run, context, cancellation)

            # Wait for both to complete
            concurrent.futures.wait([future_step, future_drawing], return_when=concurrent.futures.ALL_COMPLETED)

            step_exc = future_step.exception()
            if step_exc is not None:
                # 3D is the core dependency. If it fails, the entire job must fail.
                raise DFMError("fusion_failed", f"3D Step analysis failed: {step_exc}")
            
            all_artifacts.extend(future_step.result())

            drawing_exc = future_drawing.exception()
            if drawing_exc is None:
                all_artifacts.extend(future_drawing.result())
            # If 2D (auxiliary) fails, we swallow the error and degrade gracefully.

        return all_artifacts
