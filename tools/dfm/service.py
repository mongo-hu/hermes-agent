"""Application service behind the stable Hermes DFM tool schemas."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.context_references import parse_context_references
from hermes_constants import get_hermes_home

from .analyzers.base import AnalyzerContext, CancellationToken
from .analyzers.fusion import GLOBAL_OBSERVATION_KINDS
from .analyzers.registry import AnalyzerRegistry, build_default_registry
from .config import DFMConfig, load_dfm_config
from .contracts import (
    ArtifactRecord,
    ClarificationRecord,
    FactRecord,
    InputRecord,
    ObservationRecord,
    PlanOperation,
    PlanRecord,
    ProjectManifest,
    RunRecord,
    RunStatus,
)
from .discovery import DiscoveryEngine
from .errors import DFMError
from .feature_recognition.occt_cpp import build_occt_feature_recognition_provider
from .ontology import LocalOntologyStore
from .project.inputs import InputRegistrar
from .project.manifest import ManifestStore
from .project.workspace import DFMWorkspace
from .processes.registry import ProcessAdapterRegistry, build_default_process_registry
from .processes.occt_injection import (
    compile_occt_injection_plan,
    preview_operations,
)
from .runtime.jobs import JobManager
from .viewer import materialize_preview_manifest


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_input_path(raw_path: object, working_dir: object = None) -> Path:
    value = str(raw_path or "").strip()
    references = parse_context_references(value)
    if (
        len(references) == 1
        and references[0].kind == "file"
        and references[0].raw == value
    ):
        value = references[0].target
    elif len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        value = value[1:-1]

    path = Path(os.path.expanduser(value))
    if not path.is_absolute() and working_dir:
        path = Path(str(working_dir)).expanduser() / path
    return path.resolve()


class DFMService:
    _AGENT_INTERPRETATION_VERSION = "1.0.0"
    _AGENT_OBSERVATION_PROVIDER = "hermes_agent_event_loop"
    _OBSERVATION_KIND_GUIDANCE = (
        "material",
        "general_tolerance",
        "surface_finish",
        "part_name",
        "manufacturing_constraint",
        "thread_requirement",
        "global_note",
        "dimension",
        "tolerance",
        "wall_thickness",
        "draft_angle",
        "radius",
        "hole_diameter",
        "hole_depth",
    )
    _FACTS_REQUIRING_NORMALIZATION = {"pull_dir"}
    _FACT_ALIASES = {
        "unit": "model_units",
        "units": "model_units",
        "model_unit": "model_units",
        "model_units": "model_units",
        "pull_direction": "pull_dir",
        "mold_pull_direction": "pull_dir",
        "pull_dir": "pull_dir",
        "material": "material",
        "process": "process",
        "manufacturing_process": "process",
    }

    def __init__(
        self,
        *,
        config: DFMConfig | None = None,
        workspace: DFMWorkspace | None = None,
        registry: AnalyzerRegistry | None = None,
        process_registry: ProcessAdapterRegistry | None = None,
        ontology_store: LocalOntologyStore | None = None,
        reconcile_jobs: bool = True,
    ) -> None:
        self.config = config or load_dfm_config()
        self.workspace = workspace or DFMWorkspace()
        self.registry = registry or build_default_registry(self.config)
        self.ontology_store = ontology_store or LocalOntologyStore(
            self.workspace.root / "ontology" / "dfm-ontology.sqlite3"
        )
        self.ontology_store.ensure_bootstrap(
            Path(__file__).resolve().parent
            / "scopes"
            / "injection"
            / "ontology_snapshot_v2.json"
        )
        self.process_registry = process_registry or build_default_process_registry(
            self.ontology_store
        )
        self.inputs = InputRegistrar(self.workspace, self.config)
        registry_keys = set(self.registry.keys())
        self.discovery = DiscoveryEngine(
            ontology_store=self.ontology_store,
            geometry_provider=build_occt_feature_recognition_provider(
                self.config.geometry_executable,
                timeout_seconds=self.config.geometry_timeout_seconds,
            ),
            drawing_provider_version=(
                self.registry.get("drawing").version
                if "drawing" in registry_keys
                else "not_registered"
            ),
            fusion_provider_version=(
                self.registry.get("fusion").version
                if "fusion" in registry_keys
                else "not_registered"
            ),
        )
        self.jobs = JobManager(
            self.workspace, self.registry, self.config, reconcile=reconcile_jobs
        )

    def _store(self, project_id: str) -> ManifestStore:
        return ManifestStore(self.workspace.project_dir(project_id))

    def _context(
        self, manifest: ProjectManifest, plan: PlanRecord | None = None
    ) -> AnalyzerContext:
        return AnalyzerContext(
            manifest.project_id,
            self.workspace.project_dir(manifest.project_id),
            manifest.input_mode,
            manifest.inputs,
            plan=plan,
        )

    def _materialize_step_preview(
        self,
        manifest: ProjectManifest,
        input_record: InputRecord,
    ) -> dict[str, Any]:
        """Build a 3D preview without replacing the durable PythonOCC/NX paths."""

        if input_record.kind != "step":
            return {"status": "not_applicable"}
        try:
            run_id = f"preview_{input_record.sha256[:16]}"
            plan = PlanRecord(
                plan_id=run_id,
                input_mode="step",
                analyzer_keys=["occt_cpp"],
                status="preview",
                created_at=_utc_now(),
                process="injection",
                process_adapter_version="occt-preview-v1",
                scope_id="injection.geometry-core",
                scope_version="4.0.0",
                input_ids=[input_record.input_id],
                input_hashes={input_record.input_id: input_record.sha256},
                operations=preview_operations(),
            )
            context = AnalyzerContext(
                manifest.project_id,
                self.workspace.project_dir(manifest.project_id),
                manifest.input_mode,
                manifest.inputs,
                run_id=run_id,
                plan=plan,
            )
            artifacts = self.registry.get("occt_cpp").run(
                context, CancellationToken()
            )
            viewer = materialize_preview_manifest(
                context.project_dir,
                run_id,
                input_record.sha256,
                artifacts,
            )
            if viewer is None:
                raise DFMError(
                    "preview_artifact_missing",
                    "The OCCT preview did not produce a render scene and topology map.",
                )
            return {
                "status": "ready",
                "run_id": run_id,
                "viewer_manifest": str(
                    (context.project_dir / viewer.relative_path).resolve()
                ),
            }
        except DFMError as exc:
            # Registration remains durable when the optional executable is absent.
            return {"status": "unavailable", "error": exc.to_dict()["error"]}

    @staticmethod
    def _canonical_fact_name(fact_name: str) -> str:
        normalized = (
            str(fact_name or "").strip().lower().replace("-", "_").replace(" ", "_")
        )
        return DFMService._FACT_ALIASES.get(normalized, normalized)

    @staticmethod
    def _normalize_fact_value(fact_name: str, raw_value: Any) -> Any:
        """Normalize fact values that may be serialized as JSON strings.

        Some facts (like pull_dir) require array values, but tool parameters
        are JSON-serialized. This method parses them back to proper types.

        Args:
            fact_name: The name of the fact being confirmed.
            raw_value: The raw value from the tool call (may be a JSON string).

        Returns:
            The normalized value (parsed if necessary).
        """
        if fact_name not in DFMService._FACTS_REQUIRING_NORMALIZATION:
            return raw_value

        # If already a list/tuple, return as-is
        if isinstance(raw_value, (list, tuple)):
            return raw_value

        # If a string, try to parse as JSON
        if isinstance(raw_value, str):
            try:
                import json

                parsed = json.loads(raw_value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass

        return raw_value

    @staticmethod
    def _active_inputs(manifest: ProjectManifest):
        superseded = {
            item.supersedes_input_id
            for item in manifest.inputs
            if item.supersedes_input_id
        }
        return [item for item in manifest.inputs if item.input_id not in superseded]

    @staticmethod
    def _operation_closure(operations, affected_ids: list[str]):
        if not affected_ids:
            return list(operations)
        by_id = {item.operation_id: item for item in operations}
        required = set()

        def include(operation_id: str) -> None:
            if operation_id in required or operation_id not in by_id:
                return
            required.add(operation_id)
            for dependency in by_id[operation_id].depends_on:
                include(dependency)

        for operation_id in affected_ids:
            include(operation_id)
        return [item for item in operations if item.operation_id in required]

    @staticmethod
    def _affected_operations_for_fact(plan: PlanRecord, fact_name: str) -> list[str]:
        """Return the directly affected operations plus their downstream consumers."""

        if fact_name == "process":
            return [item.operation_id for item in plan.operations]
        affected = {
            item.operation_id
            for item in plan.operations
            if fact_name in item.required_fact_names
        }
        affected.update(
            operand.operation_id
            for item in plan.rule_bindings
            if fact_name in item.required_fact_names
            for operand in item.measurement_operands()
        )
        changed = True
        while changed:
            changed = False
            for operation in plan.operations:
                if operation.operation_id not in affected and affected.intersection(
                    operation.depends_on
                ):
                    affected.add(operation.operation_id)
                    changed = True
        return [
            item.operation_id
            for item in plan.operations
            if item.operation_id in affected
        ]

    def _capabilities(self, manifest: ProjectManifest) -> dict[str, dict[str, Any]]:
        context = self._context(manifest)
        return {
            key: self.registry.get(key).capability(context).to_dict()
            for key in self.registry.keys()
        }

    @staticmethod
    def _read_ndjson(path: Path) -> list[dict[str, Any]]:
        try:
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DFMError(
                "drawing_artifact_invalid",
                "The persisted drawing OCR fragments cannot be read.",
                {"path": str(path)},
            ) from exc

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _drawing_fragment_artifact(
        self, manifest: ProjectManifest, input_id: str
    ) -> ArtifactRecord:
        artifact = next(
            (
                item
                for item in reversed(manifest.artifacts)
                if item.kind == "drawing_ocr_fragments"
                and f":{input_id}:" in item.logical_id
            ),
            None,
        )
        if artifact is None:
            raise DFMError(
                "drawing_context_unavailable",
                "Run drawing OCR discovery before requesting Agent interpretation context.",
                {"input_id": input_id},
            )
        return artifact

    def _pending_drawing_interpretations(self, manifest: ProjectManifest) -> list[str]:
        state = (manifest.capabilities.get("drawing_discovery") or {}).get("inputs", {})
        return [
            item.input_id
            for item in self._active_inputs(manifest)
            if item.kind == "drawing"
            and (state.get(item.input_id) or {}).get("interpretation_status")
            != "completed"
        ]

    def _drawing_context(
        self, project_id: str, input_id: str, page: object = None
    ) -> dict[str, Any]:
        manifest = self._refresh_drawing_ocr(project_id)
        active_drawings = [
            item for item in self._active_inputs(manifest) if item.kind == "drawing"
        ]
        if not input_id and len(active_drawings) == 1:
            input_id = active_drawings[0].input_id
        input_record = next(
            (item for item in active_drawings if item.input_id == input_id), None
        )
        if input_record is None:
            raise DFMError(
                "drawing_input_missing",
                "drawing_context requires one active drawing input_id.",
                {"available_input_ids": [item.input_id for item in active_drawings]},
            )
        page_number = None
        if page is not None:
            if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
                raise DFMError(
                    "drawing_context_invalid",
                    "drawing_context page must be a positive integer.",
                )
            page_number = page
        artifact = self._drawing_fragment_artifact(manifest, input_record.input_id)
        artifact_path = self.workspace.project_dir(project_id) / artifact.relative_path
        fragments = self._read_ndjson(artifact_path)
        if page_number is not None:
            fragments = [item for item in fragments if item.get("page") == page_number]
        truncated = len(fragments) > 200
        fragments = fragments[:200]
        return {
            "ok": True,
            "project_id": project_id,
            "revision": manifest.revision,
            "input": input_record.to_dict(),
            "fragment_artifact": artifact.to_dict(),
            "page": page_number,
            "fragments": fragments,
            "truncated": truncated,
            "interpretation_contract": {
                "provider": self._AGENT_OBSERVATION_PROVIDER,
                "version": self._AGENT_INTERPRETATION_VERSION,
                "allowed_kind_pattern": "^[a-z][a-z0-9_.-]{0,99}$",
                "kind_guidance": list(self._OBSERVATION_KIND_GUIDANCE),
                "required_fields": [
                    "kind",
                    "value",
                    "confidence",
                    "source_fragment_refs",
                ],
                "rules": [
                    "Extract only explicitly stated drawing facts.",
                    "Every observation must cite one or more returned fragment_id values.",
                    "Do not create feature_refs, region_refs, IDs, or confirmed status.",
                    "Submit an empty observations list when no explicit fact is present.",
                ],
            },
        }

    def _validate_agent_observations(
        self,
        manifest: ProjectManifest,
        input_id: str,
        proposals: object,
    ) -> tuple[list[ObservationRecord], ArtifactRecord]:
        if not isinstance(proposals, list) or len(proposals) > 200:
            raise DFMError(
                "observation_submission_invalid",
                "observations must be an array containing at most 200 proposals.",
            )
        input_record = next(
            (
                item
                for item in self._active_inputs(manifest)
                if item.input_id == input_id and item.kind == "drawing"
            ),
            None,
        )
        if input_record is None:
            raise DFMError(
                "drawing_input_missing",
                "submit_observations requires one active drawing input_id.",
                {"input_id": input_id},
            )
        fragment_artifact = self._drawing_fragment_artifact(manifest, input_id)
        fragments = self._read_ndjson(
            self.workspace.project_dir(manifest.project_id)
            / fragment_artifact.relative_path
        )
        fragment_by_id = {
            str(item.get("fragment_id")): item
            for item in fragments
            if item.get("fragment_id")
        }
        allowed = {
            "kind",
            "value",
            "unit",
            "confidence",
            "source_fragment_refs",
        }
        observations: list[ObservationRecord] = []
        seen: set[str] = set()
        for index, proposal in enumerate(proposals):
            if not isinstance(proposal, dict) or set(proposal) - allowed:
                raise DFMError(
                    "observation_submission_invalid",
                    "An observation proposal contains unsupported fields.",
                    {"index": index, "allowed_fields": sorted(allowed)},
                )
            kind = str(proposal.get("kind") or "").strip().lower().replace(" ", "_")
            value = proposal.get("value")
            unit = proposal.get("unit")
            source_fragment_refs = proposal.get("source_fragment_refs")
            raw_confidence = proposal.get("confidence")
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", kind):
                raise DFMError(
                    "observation_kind_invalid",
                    "Observation kind must be a canonical lower-case identifier.",
                    {"index": index, "kind": kind},
                )
            if (
                value is None
                or isinstance(value, (dict, list))
                or (isinstance(value, str) and not value.strip())
            ):
                raise DFMError(
                    "observation_value_invalid",
                    "Observation value must be one explicit scalar drawing value.",
                    {"index": index},
                )
            if unit is not None and (
                not isinstance(unit, str) or not unit.strip() or len(unit) > 32
            ):
                raise DFMError(
                    "observation_unit_invalid",
                    "Observation unit must be null or a short non-empty string.",
                    {"index": index},
                )
            if (
                isinstance(raw_confidence, bool)
                or not isinstance(raw_confidence, (int, float))
                or not 0 <= float(raw_confidence) <= 1
            ):
                raise DFMError(
                    "observation_confidence_invalid",
                    "Observation confidence must be a number between zero and one.",
                    {"index": index},
                )
            if (
                not isinstance(source_fragment_refs, list)
                or not 1 <= len(source_fragment_refs) <= 20
                or len(source_fragment_refs) != len(set(source_fragment_refs))
                or any(
                    not isinstance(item, str) or item not in fragment_by_id
                    for item in source_fragment_refs
                )
            ):
                raise DFMError(
                    "observation_evidence_invalid",
                    "Every observation must cite one to twenty unique OCR fragment IDs from the selected drawing.",
                    {"index": index},
                )
            sources = [fragment_by_id[item] for item in source_fragment_refs]
            confidence = min(
                float(raw_confidence),
                min(float(item.get("confidence") or 0.0) for item in sources),
            )
            identity = json.dumps(
                {
                    "input_id": input_id,
                    "kind": kind,
                    "value": value,
                    "unit": unit,
                    "source_fragment_refs": source_fragment_refs,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            observation_id = f"observation.agent.{digest[:20]}"
            if observation_id in seen:
                raise DFMError(
                    "observation_submission_duplicate",
                    "The observation submission contains a duplicate proposal.",
                    {"index": index, "observation_id": observation_id},
                )
            seen.add(observation_id)
            observations.append(
                ObservationRecord(
                    observation_id=observation_id,
                    input_id=input_id,
                    kind=kind,
                    value=value.strip() if isinstance(value, str) else value,
                    source_refs=[
                        f"artifact:{fragment_artifact.artifact_id}#fragment={item}"
                        for item in source_fragment_refs
                    ],
                    confidence=confidence,
                    status="candidate",
                    unit=unit.strip() if isinstance(unit, str) else None,
                    provenance={
                        "provider": self._AGENT_OBSERVATION_PROVIDER,
                        "provider_version": self._AGENT_INTERPRETATION_VERSION,
                        "source_type": "drawing_recognition",
                        "input_sha256": input_record.sha256,
                        "fragment_refs": list(source_fragment_refs),
                        "pages": sorted({
                            int(item["page"])
                            for item in sources
                            if item.get("page") is not None
                        }),
                        "original_text": "\n".join(
                            str(item.get("text") or "") for item in sources
                        )[:1000],
                    },
                )
            )
        return observations, fragment_artifact

    @staticmethod
    def _invalidate_plans_for_semantics(current: ProjectManifest, reason: str):
        return [
            replace(
                plan,
                status="invalidated",
                invalidated_by=reason,
                affected_operation_ids=[item.operation_id for item in plan.operations],
            )
            if plan.phase == "analysis" and plan.status != "invalidated"
            else plan
            for plan in current.plans
        ]

    def _materialize_agent_observations(
        self,
        manifest: ProjectManifest,
        input_id: str,
        observations: list[ObservationRecord],
    ) -> ArtifactRecord:
        input_record = next(
            item for item in manifest.inputs if item.input_id == input_id
        )
        relative_dir = Path("discovery") / "drawing" / input_record.sha256[:16]
        output_dir = self.workspace.project_dir(manifest.project_id) / relative_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (
            f"drawing_{input_record.sha256[:16]}_agent_observations.jsonl"
        )
        output_path.write_text(
            "".join(
                json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
                for item in observations
            ),
            encoding="utf-8",
            newline="\n",
        )
        digest = self._file_sha256(output_path)
        return ArtifactRecord(
            artifact_id=f"artifact_drawing-observations_{digest[:16]}",
            kind="drawing_observations",
            relative_path=(relative_dir / output_path.name).as_posix(),
            media_type="application/x-ndjson",
            created_at=_utc_now(),
            logical_id=(
                f"drawing-observations:{input_id}:{self._AGENT_INTERPRETATION_VERSION}"
            ),
            size_bytes=output_path.stat().st_size,
            sha256=digest,
        )

    def _submit_observations(
        self,
        project_id: str,
        input_id: str,
        proposals: object,
        expected_revision: object,
    ) -> dict[str, Any]:
        manifest = self._refresh_drawing_ocr(project_id)
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise DFMError(
                "manifest_revision_required",
                "submit_observations requires the revision returned by drawing_context.",
            )
        observations, _fragment_artifact = self._validate_agent_observations(
            manifest, input_id, proposals
        )
        observation_artifact = self._materialize_agent_observations(
            manifest, input_id, observations
        )

        def submit(current: ProjectManifest) -> ProjectManifest:
            previous_ids = {
                item.observation_id
                for item in current.observations
                if item.input_id == input_id
                and item.provenance.get("provider") == self._AGENT_OBSERVATION_PROVIDER
            }
            capabilities = dict(current.capabilities)
            drawing = dict(capabilities.get("drawing_discovery") or {})
            inputs = dict(drawing.get("inputs") or {})
            input_state = dict(inputs.get(input_id) or {})
            input_state.update({
                "interpretation_status": "completed",
                "interpretation_provider": self._AGENT_OBSERVATION_PROVIDER,
                "interpretation_version": self._AGENT_INTERPRETATION_VERSION,
                "observation_count": len(observations),
            })
            inputs[input_id] = input_state
            drawing["inputs"] = inputs
            capabilities["drawing_discovery"] = drawing
            capabilities.pop("fusion_review", None)
            return replace(
                current,
                observations=[
                    item
                    for item in current.observations
                    if not (
                        item.input_id == input_id
                        and item.provenance.get("provider")
                        == self._AGENT_OBSERVATION_PROVIDER
                    )
                ]
                + observations,
                fusion_links=[
                    item
                    for item in current.fusion_links
                    if not previous_ids.intersection(item.observation_refs)
                ],
                artifacts=[
                    item
                    for item in current.artifacts
                    if item.logical_id != observation_artifact.logical_id
                ]
                + [observation_artifact],
                capabilities=capabilities,
                plans=self._invalidate_plans_for_semantics(
                    current, "drawing_interpretation"
                ),
                updated_at=_utc_now(),
            )

        updated = self._store(project_id).update(
            submit, expected_revision=expected_revision
        )
        updated = self._apply_drawing_source_policies(project_id)
        accepted = [
            item.to_dict()
            for item in updated.observations
            if item.input_id == input_id
            and item.provenance.get("provider") == self._AGENT_OBSERVATION_PROVIDER
        ]
        return {
            "ok": True,
            "project_id": project_id,
            "revision": updated.revision,
            "observations": accepted,
            "artifact": observation_artifact.to_dict(),
            "next_action": "discover",
        }

    def _agent_drawing_observations(
        self, manifest: ProjectManifest
    ) -> list[ObservationRecord]:
        drawing_input_ids = {
            item.input_id
            for item in self._active_inputs(manifest)
            if item.kind == "drawing"
        }
        return [
            item
            for item in manifest.observations
            if item.input_id in drawing_input_ids
            and item.provenance.get("provider") == self._AGENT_OBSERVATION_PROVIDER
            and item.provenance.get("source_type") == "drawing_recognition"
            and item.kind not in GLOBAL_OBSERVATION_KINDS
            and item.status not in {"rejected", "conflict"}
        ]

    def _fusion_review_digest(self, manifest: ProjectManifest) -> str:
        payload = {
            "observations": [
                item.to_dict() for item in self._agent_drawing_observations(manifest)
            ],
            "features": [item.to_dict() for item in manifest.features],
            "regions": [item.to_dict() for item in manifest.regions],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _persist_geometry_candidates(self, project_id: str) -> ProjectManifest:
        store = self._store(project_id)
        current = store.load()
        refreshed = self.discovery.refresh_candidates(
            current, project_dir=self.workspace.project_dir(project_id)
        )
        if (
            refreshed.features == current.features
            and refreshed.regions == current.regions
            and refreshed.artifacts == current.artifacts
            and refreshed.capabilities == current.capabilities
        ):
            return current
        return store.update(
            lambda latest: replace(
                latest,
                features=refreshed.features,
                regions=refreshed.regions,
                artifacts=refreshed.artifacts,
                capabilities=refreshed.capabilities,
                updated_at=_utc_now(),
            ),
            expected_revision=current.revision,
        )

    def _fusion_review_required(self, manifest: ProjectManifest) -> bool:
        if not any(
            item.kind in {"step", "parasolid"} for item in self._active_inputs(manifest)
        ):
            return False
        local_observations = self._agent_drawing_observations(manifest)
        if not local_observations:
            return False
        review = manifest.capabilities.get("fusion_review") or {}
        return not (
            review.get("status") == "completed"
            and review.get("digest") == self._fusion_review_digest(manifest)
        )

    def _fusion_context(self, project_id: str) -> dict[str, Any]:
        manifest = self._persist_geometry_candidates(project_id)
        observations = self._agent_drawing_observations(manifest)
        return {
            "ok": True,
            "project_id": project_id,
            "revision": manifest.revision,
            "review_digest": self._fusion_review_digest(manifest),
            "observations": [item.to_dict() for item in observations],
            "features": [
                {
                    "feature_id": item.feature_id,
                    "kind": item.kind,
                    "region_refs": item.region_refs,
                    "confidence": item.confidence,
                    "status": item.status,
                    "recognizer": item.recognizer,
                    "fallback": bool(item.properties.get("fallback")),
                }
                for item in manifest.features
            ],
            "regions": [
                {
                    "region_id": item.region_id,
                    "role": item.role,
                    "semantic_label": item.semantic_label,
                    "mode": item.mode,
                    "feature_refs": item.feature_refs,
                    "geometry_ref_count": len(item.geometry_refs),
                }
                for item in manifest.regions
            ],
            "fusion_contract": {
                "required_fields": [
                    "observation_refs",
                    "feature_refs",
                    "region_refs",
                    "confidence",
                ],
                "rules": [
                    "Link only local observations whose target is explicit in the drawing.",
                    "Use only returned observation, feature, and region IDs.",
                    "Submit an empty fusion_links list when no defensible link exists.",
                    "The service derives status and validates geometry consistency.",
                ],
            },
        }

    def _submit_fusion_links(
        self,
        project_id: str,
        proposals: object,
        expected_revision: object,
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise DFMError(
                "manifest_revision_required",
                "submit_fusion_links requires the revision returned by fusion_context.",
            )
        if not isinstance(proposals, list):
            raise DFMError(
                "fusion_submission_invalid", "fusion_links must be an array."
            )
        manifest = self._store(project_id).load()
        if manifest.revision != expected_revision:
            raise DFMError(
                "manifest_conflict",
                "The DFM project changed after fusion_context was read.",
                {"expected": expected_revision, "actual": manifest.revision},
            )
        analyzer = self.registry.get("fusion")
        if not hasattr(analyzer, "validate_agent_proposals"):
            raise DFMError(
                "fusion_contract_missing",
                "The configured fusion analyzer cannot validate Agent proposals.",
            )
        links = analyzer.validate_agent_proposals(manifest, proposals)
        digest = self._fusion_review_digest(manifest)

        def submit(current: ProjectManifest) -> ProjectManifest:
            capabilities = dict(current.capabilities)
            capabilities["fusion_review"] = {
                "status": "completed",
                "digest": digest,
                "provider": self._AGENT_OBSERVATION_PROVIDER,
                "provider_version": self._AGENT_INTERPRETATION_VERSION,
                "link_count": len(links),
            }
            return replace(
                current,
                fusion_links=[
                    item
                    for item in current.fusion_links
                    if item.diagnostics.get("provider") != "hermes_agent_fusion"
                ]
                + links,
                capabilities=capabilities,
                plans=self._invalidate_plans_for_semantics(current, "fusion_review"),
                updated_at=_utc_now(),
            )

        updated = self._store(project_id).update(
            submit, expected_revision=expected_revision
        )
        return {
            "ok": True,
            "project_id": project_id,
            "revision": updated.revision,
            "fusion_links": [item.to_dict() for item in links],
            "geometry_validation": "completed",
            "next_action": "discover",
        }

    def _record_drawing_discovery_status(
        self,
        project_id: str,
        input_id: str,
        status: str,
        details: dict[str, Any],
    ) -> ProjectManifest:
        def update(current: ProjectManifest) -> ProjectManifest:
            capabilities = dict(current.capabilities)
            drawing = dict(capabilities.get("drawing_discovery") or {})
            inputs = dict(drawing.get("inputs") or {})
            inputs[input_id] = {"status": status, **details}
            drawing.update({"status": status, "inputs": inputs})
            capabilities["drawing_discovery"] = drawing
            return replace(current, capabilities=capabilities, updated_at=_utc_now())

        return self._store(project_id).update(update)

    def _refresh_drawing_ocr(self, project_id: str) -> ProjectManifest:
        store = self._store(project_id)
        manifest = store.load()
        drawing_inputs = [
            item for item in self._active_inputs(manifest) if item.kind == "drawing"
        ]
        if not drawing_inputs:
            return manifest
        analyzer = self.registry.get("drawing")
        if not hasattr(analyzer, "discover_input"):
            raise DFMError(
                "drawing_contract_missing",
                "The configured drawing analyzer does not implement OCR discovery.",
            )

        for input_record in drawing_inputs:
            manifest = store.load()
            cache_identity = str(getattr(analyzer, "cache_identity", analyzer.version))
            cache_logical_id = (
                f"drawing-diagnostics:{input_record.input_id}:"
                f"{analyzer.version}:{cache_identity}"
            )
            if any(
                artifact.logical_id == cache_logical_id
                for artifact in manifest.artifacts
            ):
                continue
            context = AnalyzerContext(
                manifest.project_id,
                self.workspace.project_dir(manifest.project_id),
                manifest.input_mode,
                [input_record],
            )
            capability = analyzer.capability(context)
            if capability.status.value != "available":
                manifest = self._record_drawing_discovery_status(
                    project_id,
                    input_record.input_id,
                    "degraded" if manifest.input_mode == "fusion" else "blocked",
                    {
                        "code": capability.error_code or capability.status.value,
                        "message": capability.reason,
                        "details": capability.details,
                    },
                )
                if manifest.input_mode == "drawing":
                    raise DFMError(
                        capability.error_code or capability.status.value,
                        capability.reason,
                        capability.details,
                    )
                continue
            try:
                batch = analyzer.discover_input(
                    context, input_record, CancellationToken()
                )
            except DFMError as exc:
                manifest = self._record_drawing_discovery_status(
                    project_id,
                    input_record.input_id,
                    "degraded" if manifest.input_mode == "fusion" else "blocked",
                    {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                )
                if manifest.input_mode == "drawing":
                    raise
                continue

            def merge(current: ProjectManifest) -> ProjectManifest:
                previous_observation_ids = {
                    item.observation_id
                    for item in current.observations
                    if item.input_id == input_record.input_id
                }
                observations = [
                    item
                    for item in current.observations
                    if item.input_id != input_record.input_id
                ]
                artifacts = [
                    item
                    for item in current.artifacts
                    if f":{input_record.input_id}:" not in item.logical_id
                ]
                fusion_links = [
                    item
                    for item in current.fusion_links
                    if not previous_observation_ids.intersection(item.observation_refs)
                ]
                capabilities = dict(current.capabilities)
                drawing = dict(capabilities.get("drawing_discovery") or {})
                inputs = dict(drawing.get("inputs") or {})
                inputs[input_record.input_id] = {
                    "status": "completed",
                    "provider_version": analyzer.version,
                    "ocr_fragment_count": batch.fragment_count,
                    "interpretation_status": "pending_agent",
                    "diagnostics": batch.diagnostics,
                }
                drawing.update({"status": "completed", "inputs": inputs})
                capabilities["drawing_discovery"] = drawing
                capabilities.pop("fusion_review", None)
                return replace(
                    current,
                    observations=observations,
                    fusion_links=fusion_links,
                    artifacts=[*artifacts, *batch.artifacts],
                    capabilities=capabilities,
                    plans=self._invalidate_plans_for_semantics(
                        current, "drawing_ocr_refresh"
                    ),
                    updated_at=_utc_now(),
                )

            manifest = store.update(merge)
        return self._apply_drawing_source_policies(project_id)

    def _apply_drawing_source_policies(self, project_id: str) -> ProjectManifest:
        manifest = self._store(project_id).load()
        policies = self.ontology_store.factor_source_policies(
            manifest.process or "injection"
        )
        if not policies:
            return manifest

        def apply(current: ProjectManifest) -> ProjectManifest:
            confirmed = {
                item.name: item for item in current.facts if item.status == "confirmed"
            }
            facts = list(current.facts)
            observations = []
            changed = False
            for observation in current.observations:
                if observation.provenance.get("source_type") != "drawing_recognition":
                    observations.append(observation)
                    continue
                policy = policies.get(observation.kind)
                if not policy or "drawing_recognition" not in set(
                    policy.get("allowed_sources", [])
                ):
                    observations.append(observation)
                    continue
                existing = confirmed.get(observation.kind)
                status = observation.status
                if existing is not None:
                    status = (
                        "confirmed"
                        if existing.value == observation.value
                        else "conflict"
                    )
                else:
                    minimum = policy.get("min_confidence")
                    evidence_ok = not policy.get("evidence_required") or bool(
                        observation.source_refs
                    )
                    confidence_ok = minimum is None or observation.confidence >= float(
                        minimum
                    )
                    if (
                        evidence_ok
                        and confidence_ok
                        and "drawing_recognition"
                        in set(policy.get("auto_accept_sources", []))
                    ):
                        identity = (
                            f"{observation.kind}:{observation.value}:"
                            f"{observation.observation_id}"
                        )
                        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                        fact = FactRecord(
                            fact_id=f"fact_drawing_{digest[:16]}",
                            name=observation.kind,
                            value=observation.value,
                            source="drawing_recognition",
                            status="confirmed",
                            unit=observation.unit,
                            evidence_refs=list(observation.source_refs),
                        )
                        facts.append(fact)
                        confirmed[fact.name] = fact
                        status = "confirmed"
                    elif (
                        evidence_ok
                        and confidence_ok
                        and "drawing_recognition"
                        in set(policy.get("confirmation_required_sources", []))
                    ):
                        status = "needs_confirmation"
                    else:
                        status = "candidate"
                updated = replace(observation, status=status)
                observations.append(updated)
                changed = changed or updated != observation
            if not changed and facts == current.facts:
                return current
            return replace(
                current,
                facts=facts,
                observations=observations,
                updated_at=_utc_now(),
            )

        store = self._store(project_id)
        current = store.load()
        preview = apply(current)
        if preview == current:
            return current
        return store.update(apply, expected_revision=current.revision)

    def _resolve_fusion_links(self, manifest: ProjectManifest) -> ProjectManifest:
        if not manifest.observations or not any(
            item.kind == "drawing" for item in self._active_inputs(manifest)
        ):
            return manifest
        analyzer = self.registry.get("fusion")
        if not hasattr(analyzer, "resolve"):
            raise DFMError(
                "fusion_contract_missing",
                "The configured fusion analyzer does not implement FusionLink resolution.",
            )
        generated = analyzer.resolve(manifest)
        preserved = [
            item
            for item in manifest.fusion_links
            if item.diagnostics.get("provider") != "hermes_fusion_resolver"
        ]
        return replace(
            manifest,
            fusion_links=[*preserved, *generated],
            updated_at=_utc_now(),
        )

    def _objective_analyzer_key(
        self, manifest: ProjectManifest, requested: object = None
    ) -> str:
        requested_key = str(requested or "").strip()
        if requested_key and requested_key not in {"fusion", "geometry"}:
            return requested_key
        geometry_kinds = {
            item.kind
            for item in self._active_inputs(manifest)
            if item.kind in {"step", "parasolid"}
        }
        if "step" in geometry_kinds:
            return self.config.geometry_backend
        if "parasolid" in geometry_kinds:
            return "parasolid"
        if manifest.input_mode == "drawing":
            return "drawing"
        raise DFMError(
            "geometry_input_required",
            "A STEP or approved Parasolid input is required for geometry checks.",
        )

    def _open_clarifications(
        self,
        manifest: ProjectManifest,
        process: str | None = None,
        phase: str = "all",
    ) -> list[ClarificationRecord]:
        # Reserved Parasolid-only projects do not ask engineering questions
        # before an approved reader exists. Mixed/STEP geometry still uses the
        # selected process adapter's prerequisites.
        if manifest.input_mode not in {"step", "geometry", "fusion"} and not (
            manifest.input_mode == "parasolid" and self.config.nx_endpoint
        ):
            return []
        adapter = self.process_registry.get(
            process or manifest.process or self.config.default_process
        )
        requirements = adapter.fact_requirements()
        available_discovery_facts = self.discovery.required_fact_names()
        confirmed = {
            self._canonical_fact_name(fact.name)
            for fact in manifest.facts
            if fact.status == "confirmed"
        }
        if manifest.process_source != "default":
            confirmed.add("process")
        existing = {item.clarification_id: item for item in manifest.clarifications}
        result = []
        for requirement in requirements:
            required_in_phase = requirement.phase == phase
            if phase == "discovery" and requirement.name in available_discovery_facts:
                required_in_phase = True
            if phase != "all" and not required_in_phase:
                continue
            name, question = requirement.name, requirement.question
            if name in confirmed:
                continue
            clarification_id = f"clarification_{name}"
            item = existing.get(clarification_id)
            if item is None or item.status == "open":
                result.append(
                    item or ClarificationRecord(clarification_id, question, "open")
                )
        return result

    def _reconcile_clarifications(self, project_id: str) -> ProjectManifest:
        """Close old open clarification rows when an alias fact already exists."""
        store = self._store(project_id)

        def reconcile(current: ProjectManifest) -> ProjectManifest:
            confirmed = {
                self._canonical_fact_name(fact.name): fact
                for fact in current.facts
                if fact.status == "confirmed"
            }
            changed = False
            rows = []
            for item in current.clarifications:
                canonical = self._canonical_fact_name(
                    item.clarification_id.removeprefix("clarification_")
                )
                fact = confirmed.get(canonical)
                if fact is not None and item.status != "answered":
                    item = replace(item, status="answered", answer=fact.value)
                    changed = True
                rows.append(item)
            return replace(current, clarifications=rows) if changed else current

        return store.update(reconcile)

    def _ensure_clarifications(
        self, project_id: str, *, phase: str = "all"
    ) -> ProjectManifest:
        store = self._store(project_id)

        def ensure(current: ProjectManifest) -> ProjectManifest:
            pending = self._open_clarifications(current, phase=phase)
            known = {item.clarification_id for item in current.clarifications}
            additions = [item for item in pending if item.clarification_id not in known]
            if not additions:
                return current
            return replace(
                current,
                clarifications=[*current.clarifications, *additions],
                updated_at=_utc_now(),
            )

        return store.update(ensure)

    def _project_payload(self, manifest: ProjectManifest) -> dict[str, Any]:
        payload = manifest.to_dict()
        phase = "analysis" if manifest.discovery_snapshots else "discovery"
        payload["open_clarifications"] = [
            item.to_dict() for item in self._open_clarifications(manifest, phase=phase)
        ]
        payload["discovery_capability"] = self.discovery.capability()
        return payload

    def _latest_discovery_snapshot(self, manifest: ProjectManifest):
        _refreshed, current_snapshot = self.discovery.freeze(manifest)
        return next(
            (
                item
                for item in reversed(manifest.discovery_snapshots)
                if item.content_sha256 == current_snapshot.content_sha256
                and item.status == "frozen"
            ),
            None,
        )

    def _bind_discovery_scope(
        self,
        process_plan,
        manifest,
        snapshot,
        *,
        preserve_single_operation_ids: bool = False,
    ):
        """Expand metric templates into one operation per non-overlapping region."""

        if not process_plan.rule_bindings:
            return process_plan
        targets = self.discovery.analysis_targets(manifest, snapshot)
        templates = {
            metric_id: operation
            for operation in process_plan.operations
            for metric_id in operation.metric_ids
        }
        operations = [item for item in process_plan.operations if not item.metric_ids]
        operation_by_target = {}
        target_counts: dict[str, int] = {}
        for target in targets:
            metric_id = str(target["metric_id"])
            target_counts[metric_id] = target_counts.get(metric_id, 0) + 1
        for target in targets:
            metric_id = target["metric_id"]
            template = templates.get(metric_id)
            if template is None:
                raise DFMError(
                    "analysis_target_unsupported",
                    "A discovered region requests a metric without a declared calculator.",
                    {"metric_id": metric_id, "region_id": target["region"].region_id},
                )
            suffix = target["region"].content_sha256[:12]
            operation_id = (
                template.operation_id
                if preserve_single_operation_ids and target_counts[metric_id] == 1
                else f"{template.operation_id}.{suffix}"
            )
            operation = replace(
                template,
                operation_id=operation_id,
                feature_refs=[target["feature"].feature_id],
                region_refs=[target["region"].region_id],
            )
            operations.append(operation)
            operation_by_target[(metric_id, target["region"].region_id)] = operation

        def matches(target, selector):
            return (
                not selector.get("feature_kind")
                or selector["feature_kind"] == target["feature"].kind
            ) and (
                not selector.get("region_role")
                or selector["region_role"] == target["region"].role
            )

        bindings = []
        for binding in process_plan.rule_bindings:
            selector_map = process_plan.binding_selectors.get(binding.binding_id, {})
            primary_selector = selector_map.get(binding.operand_alias, {})
            primary_targets = [
                target
                for target in targets
                if target["metric_id"] == binding.metric_id
                and matches(target, primary_selector)
            ]
            for primary_target in primary_targets:
                primary_operation = operation_by_target[
                    (binding.metric_id, primary_target["region"].region_id)
                ]
                additional_operands = []
                for operand in binding.additional_operands:
                    selector = selector_map.get(operand.alias, {})
                    candidates = [
                        target
                        for target in targets
                        if target["metric_id"] == operand.metric_id
                        and matches(target, selector)
                    ]
                    same_feature = [
                        target
                        for target in candidates
                        if target["feature"].feature_id
                        == primary_target["feature"].feature_id
                    ]
                    if len(same_feature) == 1:
                        selected = same_feature[0]
                    elif len(candidates) == 1:
                        selected = candidates[0]
                    else:
                        raise DFMError(
                            "analysis_operand_ambiguous",
                            "A semantic rule operand did not resolve to one discovered region.",
                            {
                                "check_id": binding.check_id,
                                "operand_alias": operand.alias,
                                "candidate_region_ids": [
                                    item["region"].region_id for item in candidates
                                ],
                            },
                        )
                    operation = operation_by_target[
                        (operand.metric_id, selected["region"].region_id)
                    ]
                    additional_operands.append(
                        replace(
                            operand,
                            operation_id=operation.operation_id,
                            feature_refs=[selected["feature"].feature_id],
                            region_refs=[selected["region"].region_id],
                        )
                    )
                suffix = primary_target["region"].content_sha256[:12]
                binding_id = (
                    binding.binding_id
                    if preserve_single_operation_ids and len(primary_targets) == 1
                    else f"{binding.binding_id}.{suffix}"
                )
                bindings.append(
                    replace(
                        binding,
                        binding_id=binding_id,
                        operation_id=primary_operation.operation_id,
                        feature_refs=[primary_target["feature"].feature_id],
                        region_refs=[primary_target["region"].region_id],
                        additional_operands=additional_operands,
                    )
                )
        return replace(
            process_plan,
            operations=operations,
            rule_bindings=bindings,
        )

    def _select_process(
        self, project_id: str, process: str, source: str
    ) -> ProjectManifest:
        self.process_registry.get(process)
        return self._store(project_id).update(
            lambda current: (
                current
                if current.process == process and current.process_source == source
                else replace(
                    current,
                    process=process,
                    process_source=source,
                    domain=(
                        "injection_molding"
                        if process == "injection"
                        else "die_casting"
                        if process == "die_casting"
                        else process
                    ),
                    plans=[
                        replace(
                            plan,
                            status="invalidated",
                            invalidated_by=f"process:{process}",
                            affected_operation_ids=[
                                item.operation_id for item in plan.operations
                            ],
                        )
                        for plan in current.plans
                    ],
                    updated_at=_utc_now(),
                )
            )
        )

    @staticmethod
    def _resolve_run_id(
        manifest: ProjectManifest, requested: object, action: str
    ) -> str:
        """Recover an omitted run id without guessing across concurrent runs."""
        run_id = str(requested or "").strip()
        if run_id:
            return run_id
        if len(manifest.runs) == 1:
            return manifest.runs[0].run_id
        active = [
            run
            for run in manifest.runs
            if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}
        ]
        if len(active) == 1:
            return active[0].run_id
        raise DFMError(
            "run_id_required",
            f"dfm_analysis {action} requires run_id when this project has multiple runs.",
            {
                "action": action,
                "run_ids": [run.run_id for run in manifest.runs[-5:]],
            },
        )

    def project(self, action: str, **params: Any) -> dict[str, Any]:
        if action == "create":
            manifest = self.workspace.create_project(
                params.get("name") or "Untitled DFM project",
                params.get("idempotency_key"),
            )
            requested_process = str(params.get("process") or "").strip()
            if requested_process:
                manifest = self._select_process(
                    manifest.project_id, requested_process, "user_selected"
                )
            return {
                "ok": True,
                "project_id": manifest.project_id,
                "project": manifest.to_dict(),
            }
        if action == "list":
            projects = []
            if self.workspace.projects_dir.exists():
                for path in sorted(
                    self.workspace.projects_dir.glob("dfm_*/project_manifest.json")
                ):
                    try:
                        projects.append(ManifestStore(path.parent).load().to_dict())
                    except DFMError:
                        continue
            return {"ok": True, "projects": projects}

        project_id = params.get("project_id") or ""
        if action == "add_input":
            source_path = _resolve_input_path(
                params.get("path"), params.get("working_dir")
            )
            record = self.inputs.register(project_id, source_path)
            self._store(project_id).update(self.discovery.refresh_candidates)
            manifest = self._ensure_clarifications(project_id, phase="discovery")
            preview = self._materialize_step_preview(manifest, record)
            return {
                "ok": True,
                "project_id": project_id,
                "input": record.to_dict(),
                "open_clarifications": [
                    item.to_dict()
                    for item in self._open_clarifications(manifest, phase="discovery")
                ],
                "discovery_capability": self.discovery.capability(),
                "preview": preview,
                **(
                    {"viewer_manifest": preview["viewer_manifest"]}
                    if preview.get("status") == "ready"
                    else {}
                ),
            }
        if action == "confirm_fact":
            name = self._canonical_fact_name(str(params.get("fact_name") or ""))
            if not name:
                raise DFMError("fact_invalid", "fact_name is required.")

            # Normalize fact_value: parse JSON strings for known array parameters
            raw_value = params.get("fact_value")
            normalized_value = self._normalize_fact_value(name, raw_value)
            if name == "process":
                normalized_value = str(normalized_value or "").strip()
                if normalized_value == "injection_molding":
                    normalized_value = "injection"
                self.process_registry.get(normalized_value)

            fact = FactRecord(
                f"fact_{uuid4().hex[:16]}", name, normalized_value, "user", "confirmed"
            )

            def confirm(current: ProjectManifest) -> ProjectManifest:
                plans = []
                for plan in current.plans:
                    affected = self._affected_operations_for_fact(plan, name)
                    plans.append(
                        replace(
                            plan,
                            status="invalidated",
                            invalidated_by=f"fact:{name}",
                            affected_operation_ids=affected,
                        )
                        if affected
                        else plan
                    )
                return replace(
                    current,
                    facts=[*current.facts, fact],
                    process=normalized_value if name == "process" else current.process,
                    process_source=(
                        "user_confirmed"
                        if name == "process"
                        else current.process_source
                    ),
                    domain=(
                        "injection_molding"
                        if name == "process" and normalized_value == "injection"
                        else "die_casting"
                        if name == "process" and normalized_value == "die_casting"
                        else current.domain
                    ),
                    clarifications=[
                        replace(item, status="answered", answer=fact.value)
                        if item.clarification_id == f"clarification_{name}"
                        else item
                        for item in current.clarifications
                    ],
                    plans=plans,
                    updated_at=_utc_now(),
                )

            manifest = self._store(project_id).update(confirm)
            return {
                "ok": True,
                "project_id": project_id,
                "fact": fact.to_dict(),
                "revision": manifest.revision,
            }
        if action == "status":
            manifest = self._reconcile_clarifications(project_id)
            context = self._context(manifest)
            process_capabilities = {
                key: self.process_registry.get(key).capability(context).to_dict()
                for key in self.process_registry.keys()
            }
            return {
                "ok": True,
                "project": self._project_payload(manifest),
                "capabilities": self._capabilities(manifest),
                "process_capabilities": process_capabilities,
            }
        raise DFMError("action_invalid", f"Unsupported dfm_project action: {action}")

    def analysis(self, action: str, **params: Any) -> dict[str, Any]:
        project_id = params.get("project_id") or ""
        if action == "drawing_context":
            return self._drawing_context(
                project_id,
                str(params.get("input_id") or ""),
                params.get("page"),
            )
        if action == "submit_observations":
            return self._submit_observations(
                project_id,
                str(params.get("input_id") or ""),
                params.get("observations"),
                params.get("expected_revision"),
            )
        if action == "fusion_context":
            return self._fusion_context(project_id)
        if action == "submit_fusion_links":
            return self._submit_fusion_links(
                project_id,
                params.get("fusion_links"),
                params.get("expected_revision"),
            )
        if action == "context":
            manifest = self._store(project_id).load()
            process = str(manifest.process or self.config.default_process)
            check_id = str(params.get("check_id") or "")
            available_check_ids = self.ontology_store.check_ids(process)
            if not check_id:
                raise DFMError(
                    "ontology_check_required",
                    "DFM ontology context requires one stable check_id.",
                    {
                        "process": process,
                        "available_check_ids": list(available_check_ids),
                    },
                )
            if check_id and check_id not in available_check_ids:
                raise DFMError(
                    "ontology_check_missing",
                    "The requested DFM Check is not published for this project process.",
                    {"check_id": check_id, "process": process},
                )
            confirmed_facts = {
                item.name: {
                    "value": item.value,
                    "source_ref": f"fact:{item.fact_id}",
                }
                for item in manifest.facts
                if item.status == "confirmed"
            }
            return {
                "ok": True,
                "project_id": project_id,
                "process": process,
                "confirmed_facts": confirmed_facts,
                "checks": [self.ontology_store.check_context(check_id)],
            }
        if action == "discover":
            store = self._store(project_id)
            manifest = self._reconcile_clarifications(project_id)
            requested_process = str(params.get("process") or manifest.process or "")
            if params.get("process"):
                manifest = self._select_process(
                    project_id, requested_process, "user_selected"
                )
            manifest = self._refresh_drawing_ocr(project_id)
            manifest = self._ensure_clarifications(project_id, phase="discovery")
            pending_interpretations = self._pending_drawing_interpretations(manifest)
            if pending_interpretations:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "agent_interpretation_required",
                    "phase": "drawing_interpretation",
                    "requires_user_response": False,
                    "next_action": "drawing_context",
                    "pending_input_ids": pending_interpretations,
                    "instructions": (
                        "Use the current Hermes model to interpret bounded OCR fragments, "
                        "then call submit_observations."
                    ),
                }
            open_clarifications = self._open_clarifications(
                manifest, requested_process, phase="discovery"
            )
            if open_clarifications:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "clarification_required",
                    "phase": "discovery",
                    "requires_user_response": True,
                    "next_action": "clarify",
                    "do_not_infer": True,
                    "clarifications": [item.to_dict() for item in open_clarifications],
                    "observation_candidates": [
                        item.to_dict()
                        for item in manifest.observations
                        if item.status
                        in {"candidate", "needs_confirmation", "conflict"}
                    ],
                }
            discovered = self._persist_geometry_candidates(project_id)
            if self._fusion_review_required(discovered):
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "agent_fusion_required",
                    "phase": "drawing_geometry_fusion",
                    "requires_user_response": False,
                    "next_action": "fusion_context",
                    "review_digest": self._fusion_review_digest(discovered),
                    "instructions": (
                        "Use the current Hermes model to propose semantic links; the "
                        "service will validate all geometry references."
                    ),
                }
            discovered = self._resolve_fusion_links(discovered)
            discovered, snapshot = self.discovery.freeze(discovered)
            existing_plan = next(
                (
                    item
                    for item in reversed(discovered.plans)
                    if item.phase == "discovery"
                    and item.discovery_snapshot_refs == [snapshot.snapshot_id]
                    and item.status == "completed"
                ),
                None,
            )
            active_inputs = self._active_inputs(discovered)
            discovery_plan = existing_plan or PlanRecord(
                plan_id=f"plan_{uuid4().hex[:16]}",
                input_mode=discovered.input_mode or "geometry",
                analyzer_keys=["hermes_discovery"],
                status="completed",
                created_at=_utc_now(),
                process=discovered.process,
                process_adapter_version=self.discovery.version,
                scope_id=str(self.discovery.catalog["catalog_id"]),
                scope_version=str(self.discovery.catalog["version"]),
                input_ids=[item.input_id for item in active_inputs],
                input_hashes={item.input_id: item.sha256 for item in active_inputs},
                operations=[
                    PlanOperation(
                        "discovery.drawing_ocr",
                        "extract_drawing_ocr_fragments",
                    ),
                    PlanOperation(
                        "discovery.agent_interpretation",
                        "persist_agent_drawing_observations",
                        depends_on=["discovery.drawing_ocr"],
                    ),
                    PlanOperation(
                        "discovery.generic_geometry",
                        "recognize_ordinary_region",
                        depends_on=["discovery.agent_interpretation"],
                        required_fact_names=["model_units"],
                    ),
                    PlanOperation(
                        "discovery.process_features",
                        "recognize_process_features_placeholder",
                        depends_on=["discovery.generic_geometry"],
                        required_fact_names=["process", "model_units"],
                        feature_refs=list(snapshot.feature_refs),
                        region_refs=list(snapshot.region_refs),
                    ),
                    PlanOperation(
                        "discovery.fusion",
                        "validate_agent_fusion_links",
                        depends_on=["discovery.process_features"],
                        feature_refs=list(snapshot.feature_refs),
                        region_refs=list(snapshot.region_refs),
                    ),
                ],
                phase="discovery",
                discovery_snapshot_refs=[snapshot.snapshot_id],
                regions=[
                    item
                    for item in discovered.regions
                    if item.region_id in snapshot.region_refs
                ],
            )
            plans = (
                discovered.plans
                if existing_plan is not None
                else [*discovered.plans, discovery_plan]
            )
            manifest = store.update(
                lambda current: replace(
                    current,
                    features=discovered.features,
                    regions=discovered.regions,
                    observations=discovered.observations,
                    fusion_links=discovered.fusion_links,
                    discovery_snapshots=discovered.discovery_snapshots,
                    artifacts=discovered.artifacts,
                    capabilities=discovered.capabilities,
                    plans=plans,
                    updated_at=_utc_now(),
                )
            )
            self._ensure_clarifications(project_id, phase="analysis")
            return {
                "ok": True,
                "project_id": project_id,
                "phase": "discovery",
                "plan": discovery_plan.to_dict(),
                "snapshot": snapshot.to_dict(),
                "features": [
                    item.to_dict()
                    for item in manifest.features
                    if item.feature_id in snapshot.feature_refs
                ],
                "regions": [
                    item.to_dict()
                    for item in manifest.regions
                    if item.region_id in snapshot.region_refs
                ],
                "observations": [
                    item.to_dict()
                    for item in manifest.observations
                    if item.observation_id in snapshot.observation_refs
                ],
                "fusion_links": [
                    item.to_dict()
                    for item in manifest.fusion_links
                    if item.fusion_link_id in snapshot.fusion_link_refs
                ],
                "capability": self.discovery.capability(),
                "drawing_discovery": manifest.capabilities.get("drawing_discovery", {}),
                "open_clarifications": [
                    item.to_dict()
                    for item in self._open_clarifications(manifest, phase="analysis")
                ],
            }
        if action == "plan":
            store = self._store(project_id)
            manifest = self._reconcile_clarifications(project_id)
            if not params.get("analyzer_key") and not manifest.input_mode:
                raise DFMError(
                    "input_required", "Register a DFM input before planning analysis."
                )
            analyzer_key = self._objective_analyzer_key(
                manifest, params.get("analyzer_key")
            )
            requested_process = str(
                params.get("process") or manifest.process or self.config.default_process
            )
            manifest = self._select_process(
                project_id,
                requested_process,
                "user_selected" if params.get("process") else manifest.process_source,
            )
            manifest = self._ensure_clarifications(project_id, phase="analysis")
            discovery_snapshot = self._latest_discovery_snapshot(manifest)
            if discovery_snapshot is None:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "discovery_required",
                    "phase": "discovery",
                    "requires_user_response": False,
                    "next_action": "discover",
                }
            open_clarifications = self._open_clarifications(
                manifest, requested_process, phase="analysis"
            )
            if open_clarifications:
                return {
                    "ok": False,
                    "project_id": project_id,
                    "status": "clarification_required",
                    "requires_user_response": True,
                    "next_action": "clarify",
                    "do_not_infer": True,
                    "clarifications": [item.to_dict() for item in open_clarifications],
                }
            analyzer = self.registry.get(str(analyzer_key))
            context = self._context(manifest)
            capability = analyzer.capability(context)
            process = requested_process
            process_plan = None
            if str(analyzer_key) in {"step", "parasolid", "occt_cpp"}:
                adapter = self.process_registry.get(process)
                defaults = adapter.compile(context, {})
                raw_parameters = {
                    fact.name: {
                        "value": fact.value,
                        "source": "project_fact",
                        "source_ref": f"fact:{fact.fact_id}",
                    }
                    for fact in manifest.facts
                    if fact.status == "confirmed"
                    and fact.name in defaults.accepted_inputs
                }
                process_plan = (
                    compile_occt_injection_plan(adapter, context, raw_parameters)
                    if str(analyzer_key) == "occt_cpp" and process == "injection"
                    else adapter.compile(context, raw_parameters)
                )
                process_plan = self._bind_discovery_scope(
                    process_plan,
                    manifest,
                    discovery_snapshot,
                    preserve_single_operation_ids=str(analyzer_key) == "occt_cpp",
                )
            parent_plan_id = str(params.get("base_plan_id") or "") or None
            parent_plan = next(
                (item for item in manifest.plans if item.plan_id == parent_plan_id),
                None,
            )
            if parent_plan_id and parent_plan is None:
                raise DFMError(
                    "plan_not_found",
                    "Base DFM analysis plan was not found.",
                    {"plan_id": parent_plan_id},
                )
            if parent_plan is not None and parent_plan.status != "invalidated":
                raise DFMError(
                    "plan_not_rebuildable",
                    "Only an invalidated plan can be rebuilt incrementally.",
                    {"plan_id": parent_plan_id, "status": parent_plan.status},
                )
            operations = self._operation_closure(
                process_plan.operations if process_plan else [],
                (
                    []
                    if parent_plan
                    and parent_plan.invalidated_by
                    and not parent_plan.invalidated_by.startswith("fact:")
                    else parent_plan.affected_operation_ids
                    if parent_plan
                    else []
                ),
            )
            operation_ids = {item.operation_id for item in operations}
            rule_bindings = (
                [
                    item
                    for item in process_plan.rule_bindings
                    if {
                        operand.operation_id for operand in item.measurement_operands()
                    }.issubset(operation_ids)
                ]
                if process_plan
                else []
            )
            active_inputs = self._active_inputs(manifest)
            now = _utc_now()
            plan = PlanRecord(
                f"plan_{uuid4().hex[:16]}",
                manifest.input_mode or str(analyzer_key),
                [str(analyzer_key)],
                "ready" if capability.status.value == "available" else "blocked",
                now,
                process=process_plan.process if process_plan else "",
                process_adapter_version=process_plan.adapter_version
                if process_plan
                else "",
                scope_id=process_plan.scope_id if process_plan else "",
                scope_version=process_plan.scope_version if process_plan else "",
                ontology_snapshot_id=(
                    process_plan.ontology_snapshot_id if process_plan else ""
                ),
                ontology_snapshot_sha256=(
                    process_plan.ontology_snapshot_sha256 if process_plan else ""
                ),
                input_ids=[item.input_id for item in active_inputs],
                input_hashes={item.input_id: item.sha256 for item in active_inputs},
                rules=process_plan.rules if process_plan else {},
                rule_bindings=rule_bindings,
                operations=operations,
                parent_plan_id=parent_plan_id,
                phase="analysis",
                discovery_snapshot_refs=[discovery_snapshot.snapshot_id],
                regions=[
                    item
                    for item in manifest.regions
                    if item.region_id in discovery_snapshot.region_refs
                ],
            )
            capability = analyzer.capability(self._context(manifest, plan))
            plan = replace(
                plan,
                status="ready" if capability.status.value == "available" else "blocked",
            )
            store.update(
                lambda current: replace(
                    current, plans=[*current.plans, plan], updated_at=_utc_now()
                )
            )
            return {
                "ok": True,
                "project_id": project_id,
                "plan": plan.to_dict(),
                "capability": capability.to_dict(),
            }
        if action == "start":
            manifest = self._store(project_id).load()
            plan_id = params.get("plan_id")
            plan = next(
                (item for item in manifest.plans if item.plan_id == plan_id), None
            )
            if plan is None:
                raise DFMError(
                    "plan_not_found",
                    "DFM analysis plan was not found.",
                    {"plan_id": plan_id},
                )
            # A capability-blocked plan is still useful for surfacing the
            # analyzer's explicit dependency error. Only changed project state
            # makes a saved plan stale and unsafe to execute.
            if plan.status == "invalidated":
                raise DFMError(
                    "plan_not_ready",
                    "DFM analysis plan is no longer executable; create a new plan.",
                    {"plan_id": plan.plan_id, "status": plan.status},
                )
            progress_callback = params.get("_tool_progress_callback")
            tool_call_id = str(params.get("_tool_call_id") or "")

            def on_update(updated: RunRecord) -> None:
                if not callable(progress_callback):
                    return
                terminal = updated.status in {
                    RunStatus.SUCCEEDED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                    RunStatus.BLOCKED,
                }
                latest = updated.artifacts[-1] if updated.artifacts else None
                viewer = next(
                    (
                        item
                        for item in reversed(updated.artifacts)
                        if item.kind == "dfm_viewer"
                    ),
                    None,
                )
                latest_text = (
                    f", latest {latest.kind}: {latest.relative_path}"
                    if latest is not None
                    else ""
                )
                preview = (
                    f"DFM {updated.status.value}: {updated.stage or 'working'} "
                    f"({updated.progress_percent}%, {len(updated.artifacts)} artifacts{latest_text})"
                )
                try:
                    progress_callback(
                        "background.tool.complete"
                        if terminal
                        else "background.tool.progress",
                        "dfm_analysis",
                        preview,
                        None,
                        tool_id=tool_call_id,
                        status=updated.status.value,
                        stage=updated.stage,
                        percent=updated.progress_percent,
                        artifact_count=len(updated.artifacts),
                        latest_artifact=(latest.relative_path if latest else None),
                        latest_artifact_kind=(latest.kind if latest else None),
                        run_id=updated.run_id,
                        project_id=project_id,
                        viewer_manifest=(
                            str(
                                (
                                    self.workspace.project_dir(project_id)
                                    / viewer.relative_path
                                ).resolve()
                            )
                            if viewer is not None
                            else None
                        ),
                        is_error=updated.status in {RunStatus.FAILED, RunStatus.BLOCKED},
                    )
                except Exception:
                    return

            run = self.jobs.start(
                project_id,
                plan.analyzer_keys[0],
                plan=plan,
                idempotency_key=params.get("idempotency_key"),
                on_update=on_update,
            )
            return {
                "ok": True,
                "project_id": project_id,
                "run": self._run_dict(project_id, run),
            }
        run_id = self._resolve_run_id(
            self._store(project_id).load(), params.get("run_id"), action
        )
        if action == "status":
            run = self.jobs.status(project_id, run_id)
        elif action == "cancel":
            run = self.jobs.cancel(project_id, run_id)
        elif action == "result":
            run = self.jobs.result(project_id, run_id)
        else:
            raise DFMError(
                "action_invalid", f"Unsupported dfm_analysis action: {action}"
            )
        return {
            "ok": True,
            "project_id": project_id,
            "run": self._run_dict(project_id, run),
        }

    def _run_dict(self, project_id: str, run: RunRecord) -> dict[str, Any]:
        payload = run.to_dict()
        project_dir = self.workspace.project_dir(project_id)
        payload["artifacts"] = [
            {
                **artifact.to_dict(),
                "path": str((project_dir / artifact.relative_path).resolve()),
            }
            for artifact in run.artifacts
        ]
        payload["diagnostics"] = {
            key: str((project_dir / relative).resolve())
            for key, relative in {
                "events": run.event_log_path,
                "stdout": run.worker_stdout_path,
                "stderr": run.worker_stderr_path,
            }.items()
            if relative
        }
        return payload

    def close(self) -> None:
        self.jobs.shutdown()
        self.ontology_store.close()


_SERVICES: dict[Path, DFMService] = {}
_SERVICES_LOCK = threading.Lock()


def get_dfm_service() -> DFMService:
    home = get_hermes_home().resolve()
    with _SERVICES_LOCK:
        service = _SERVICES.get(home)
        if service is None:
            service = DFMService()
            _SERVICES[home] = service
        return service
