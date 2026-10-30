"""Application service behind the stable Hermes DFM tool schemas."""

from __future__ import annotations

import hashlib
from itertools import product
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agent.context_references import parse_context_references
from hermes_constants import get_hermes_home

from .analyzers.base import AnalyzerContext, CancellationToken
from .analyzers.fusion import GLOBAL_OBSERVATION_KINDS
from .analyzers.occt import discover_geometry_executable
from .analyzers.registry import AnalyzerRegistry, build_default_registry
from .config import DFMConfig, geometry_backend_analyzer_key, load_dfm_config
from .contracts import (
    ArtifactRecord,
    ClarificationRecord,
    FactRecord,
    FeatureRecord,
    FormingScopeRecord,
    GeometricRequest,
    GeometricFindingRecord,
    GeometricRuleBinding,
    GeometricRuleOperand,
    GeometryRef,
    InputRecord,
    ObservationRecord,
    PlanOperation,
    PlanFragmentRecord,
    PlanRecord,
    ProjectManifest,
    RegionRecord,
    ReviewGroupRecord,
    RunRecord,
    RunStatus,
)
from .discovery import DiscoveryEngine, discovery_context_identity
from .errors import DFMError
from .feature_recognition.occt_cpp import build_occt_feature_recognition_provider
from .feature_recognition.base import reconcile_worker_features
from .ontology import BackgroundOntologySync, LocalOntologyStore, OntologySynchronizer
from .project.inputs import InputRegistrar
from .project.manifest import ManifestStore
from .project.geometry_edits import GeometryEdit, geometry_identity
from .project.workspace import DFMWorkspace
from .processes.registry import ProcessAdapterRegistry, build_default_process_registry
from .processes.occt_injection import (
    compile_occt_injection_plan,
    geometry_binding_index,
)
from .reporting.html import render_html_report
from .reporting.html.template import ContractError, normalize_editorial_content
from .reference_dependencies import resolve_reference_dependencies
from .runtime.jobs import JobManager
from .runtime.process import GeometrySessionPool, geometry_capability_identity
from .review import annotation_review_groups, local_structure_review_content, local_structure_review_groups, structure_review_groups
from .geometry.artifact_metadata import inspect_geometry_file
from .viewer import (
    materialize_discovery_viewer_manifest,
    materialize_preview_manifest,
)

_LOG = logging.getLogger(__name__)


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
    _STARTUP_FACTS = (
        ("process", "请选择 DFM 分析工艺。", ()),
        ("model_units", "请确认模型单位长度。", ("mm", "inch")),
        ("pull_dir", "请确认主出模方向。", ("+X", "-X", "+Y", "-Y", "+Z", "-Z")),
    )
    _DISCOVERY_FACT_NAMES = frozenset(name for name, _, _ in _STARTUP_FACTS)
    # The published fill factor has different answer vocabularies for wall and
    # screw-boss Checks. Its confirmed answer is therefore scoped to a Check.
    _CHECK_SCOPED_FACTS = {"injection": frozenset({"mat_additive_fill"})}
    _PROCESS_ALIASES = {
        "injection": "injection",
        "injection_molding": "injection",
        "注塑成型": "injection",
        "注塑成型_(injection_molding)": "injection",
        "injection_molding_(注塑成型)": "injection",
        "注塑": "injection",
        "热塑性注塑": "injection",
        "die_casting": "die_casting",
        "diecasting": "die_casting",
        "压铸_(die_casting)": "die_casting",
        "die_casting_(压铸)": "die_casting",
        "压铸": "die_casting",
    }
    _MODEL_UNIT_ALIASES = {
        "millimeter": "mm",
        "millimeters": "mm",
        "millimeter (mm)": "mm",
        "毫米 (mm)": "mm",
        "mm (毫米)": "mm",
        "毫米": "mm",
    }
    _PULL_DIRECTIONS = {
        "+X": [1.0, 0.0, 0.0],
        "X+": [1.0, 0.0, 0.0],
        "-X": [-1.0, 0.0, 0.0],
        "X-": [-1.0, 0.0, 0.0],
        "+Y": [0.0, 1.0, 0.0],
        "Y+": [0.0, 1.0, 0.0],
        "-Y": [0.0, -1.0, 0.0],
        "Y-": [0.0, -1.0, 0.0],
        "+Z": [0.0, 0.0, 1.0],
        "Z+": [0.0, 0.0, 1.0],
        "-Z": [0.0, 0.0, -1.0],
        "Z-": [0.0, 0.0, -1.0],
    }
    _FACT_ALIASES = {
        "unit": "model_units",
        "units": "model_units",
        "model_unit": "model_units",
        "model_length_unit": "model_units",
        "model_units": "model_units",
        "tool_main_draw_dir": "pull_dir",
        "pull_direction": "pull_dir",
        "mold_pull_direction": "pull_dir",
        "pull_dir": "pull_dir",
        "material": "material",
        "process": "process",
        "manufacturing_process": "process",
    }
    _FACT_SOURCE_ALIASES = {
        "user": "USR",
        "project_metadata": "DB",
        "drawing_recognition": "DWG",
        "geometry_recognition": "GEO",
        "derived_program": "DER",
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
        self._workflow_lock = threading.RLock()
        self.workspace = workspace or DFMWorkspace()
        self.geometry_sessions = GeometrySessionPool(
            discover_geometry_executable(self.config.geometry_executable) or "",
            request_timeout_seconds=self.config.geometry_timeout_seconds,
            job_timeout_seconds=self.config.geometry_timeout_seconds,
            poll_interval_seconds=self.config.geometry_job_poll_interval_ms / 1000,
            idle_timeout_seconds=self.config.geometry_session_idle_timeout_seconds,
            max_sessions=self.config.geometry_session_max_processes,
            memory_budget_mb=self.config.geometry_session_memory_budget_mb,
        )
        self.registry = registry or build_default_registry(
            self.config, geometry_sessions=self.geometry_sessions
        )
        self.ontology_store = ontology_store or LocalOntologyStore(
            self.workspace.ontology_dir / "dfm-ontology.sqlite3"
        )
        self.ontology_store.ensure_bootstrap(
            Path(__file__).resolve().parent
            / "scopes"
            / "injection"
            / "ontology_snapshot_v2.json"
        )
        # A report/status-only service instance must not start a geometry
        # process.  The handshake is performed lazily on the project-owned
        # session when an OCCT plan is actually requested.
        self.occt_geometry_capability: dict[str, Any] | None = None
        self.ontology_synchronizer: OntologySynchronizer | None = None
        self._ontology_background_sync: BackgroundOntologySync | None = None
        if self.config.ontology_endpoint:
            self.ontology_synchronizer = OntologySynchronizer(
                store=self.ontology_store,
                root=self.workspace.ontology_dir,
                config=self.config,
            )
            try:
                current_ontology_valid = (
                    self.ontology_synchronizer.ensure_current_valid()
                )
            except DFMError:
                # A validator upgrade may invalidate a Snapshot accepted by an
                # older process. Upgrade synchronously before exposing DFM.
                current_ontology_valid = False
            if not current_ontology_valid:
                self.ontology_synchronizer.sync_once()
            self._ontology_background_sync = BackgroundOntologySync(
                self.ontology_synchronizer,
                self.config.ontology_sync_interval_seconds,
            )
            self._ontology_background_sync.start()
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
                session_pool=self.geometry_sessions,
                lease_seconds=self.config.geometry_session_lease_seconds,
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

    def _ensure_occt_geometry_capability(self, project_id: str) -> dict[str, Any]:
        owned = self.geometry_sessions.acquire(project_id)
        capability = dict(owned.session.capability)
        geometry_binding_index(capability)
        if (
            self.occt_geometry_capability is None
            or geometry_capability_identity(self.occt_geometry_capability)
            != geometry_capability_identity(capability)
        ):
            self.occt_geometry_capability = capability
            self.ontology_store.configure_geometric_bindings(
                geometry_binding_index(capability)
            )
        return capability

    def _materialize_step_preview(
        self,
        manifest: ProjectManifest,
        input_record: InputRecord,
    ) -> dict[str, Any]:
        """Open the project-owned Worker snapshot and expose its preview artifacts."""

        if input_record.kind != "step":
            return {"status": "not_applicable"}
        try:
            run_id = f"preview_{input_record.sha256[:16]}"
            project_dir = self.workspace.project_dir(manifest.project_id).resolve()
            opened = self.geometry_sessions.open_model(
                manifest.project_id,
                input_path=(project_dir / input_record.relative_path).resolve(),
                input_sha256=input_record.sha256,
                artifact_dir=(
                    project_dir
                    / "artifacts"
                    / "geometry_sessions"
                    / input_record.sha256[:16]
                ),
                lease_seconds=self.config.geometry_session_lease_seconds,
            )
            raw_artifacts = opened.get("artifacts")
            if not isinstance(raw_artifacts, dict):
                raise DFMError(
                    "geometry_protocol_invalid",
                    "Opening the Worker snapshot did not return preview artifacts.",
                )
            artifacts = []
            for kind in ("preflight", "topology_map", "render_scene"):
                path = Path(str(raw_artifacts.get(kind) or "")).resolve()
                if not path.is_file() or not path.is_relative_to(project_dir):
                    raise DFMError(
                        "geometry_artifact_invalid",
                        "A Worker preview artifact is outside the owning project.",
                        {"kind": kind},
                    )
                file_info = inspect_geometry_file(path, kind)
                artifacts.append(
                    ArtifactRecord(
                        f"artifact_{run_id}_{kind}",
                        kind,
                        path.relative_to(project_dir).as_posix(),
                        "application/json",
                        _utc_now(),
                        size_bytes=file_info.size_bytes,
                        sha256=file_info.sha256,
                    )
                )
            viewer = materialize_preview_manifest(
                project_dir,
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
                "viewer_manifest": str((project_dir / viewer.relative_path).resolve()),
                "snapshot_id": opened["snapshot_id"],
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

    @classmethod
    def _fact_reference(cls, raw: str) -> tuple[str, str | None]:
        scoped, _feature_separator, _feature_id = str(raw or "").partition("#")
        name, separator, check_id = scoped.partition("@")
        return cls._canonical_fact_name(name), check_id.strip() if separator else None

    @classmethod
    def _fact_scope_reference(cls, raw: str) -> tuple[str, str | None, str | None]:
        scoped, feature_separator, feature_id = str(raw or "").partition("#")
        name, check_id = cls._fact_reference(scoped)
        return (
            name,
            check_id,
            feature_id.strip() if feature_separator and feature_id.strip() else None,
        )

    @staticmethod
    def _clarification_id(
        fact_name: str,
        *,
        check_id: str | None = None,
        feature_id: str | None = None,
    ) -> str:
        scoped = f"{fact_name}@{check_id}" if check_id else fact_name
        if feature_id:
            scoped = f"{scoped}#{feature_id}"
        return f"clarification_{scoped}"

    @staticmethod
    def _normalize_fact_value(fact_name: str, raw_value: Any) -> Any:
        """Convert user input into the canonical fact representation once."""

        if fact_name == "process":
            key = (
                str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")
            )
            if key not in DFMService._PROCESS_ALIASES:
                raise DFMError(
                    "fact_invalid",
                    "process must be injection or die_casting",
                    {"fact_name": fact_name, "value": raw_value},
                )
            return DFMService._PROCESS_ALIASES[key]
        if fact_name == "model_units":
            key = str(raw_value or "").strip().lower()
            if not key:
                raise DFMError(
                    "fact_invalid",
                    "model_units must be a non-empty length unit",
                    {"fact_name": fact_name, "value": raw_value},
                )
            return DFMService._MODEL_UNIT_ALIASES.get(key, key)
        if fact_name != "pull_dir":
            return raw_value

        value = raw_value
        if isinstance(value, str):
            direction = DFMService._PULL_DIRECTIONS.get(value.strip().upper())
            if direction is not None:
                return list(direction)
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError) as exc:
                raise DFMError(
                    "fact_invalid",
                    "pull_dir must be +X, -X, +Y, -Y, +Z, -Z, or a 3D vector",
                    {"fact_name": fact_name, "value": raw_value},
                ) from exc
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise DFMError(
                "fact_invalid",
                "pull_dir must contain exactly three numbers",
                {"fact_name": fact_name, "value": raw_value},
            )
        try:
            vector = [float(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "fact_invalid",
                "pull_dir must contain exactly three numbers",
                {"fact_name": fact_name, "value": raw_value},
            ) from exc
        if not all(math.isfinite(item) for item in vector):
            raise DFMError(
                "fact_invalid",
                "pull_dir must contain finite numbers",
                {"fact_name": fact_name, "value": raw_value},
            )
        magnitude = math.sqrt(sum(item * item for item in vector))
        if magnitude == 0:
            raise DFMError(
                "fact_invalid",
                "pull_dir must be a non-zero vector",
                {"fact_name": fact_name, "value": raw_value},
            )
        return [item / magnitude for item in vector]

    @staticmethod
    def _factor_value_matches_schema(value: Any, schema: Any) -> bool:
        """Validate the bounded JSON-Schema subset used by published Factors."""

        if not isinstance(schema, dict):
            return True
        if "enum" in schema and value not in schema["enum"]:
            return False
        expected = schema.get("type")
        type_checks = {
            "string": lambda item: isinstance(item, str),
            "number": lambda item: (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(float(item))
            ),
            "integer": lambda item: (
                isinstance(item, int) and not isinstance(item, bool)
            ),
            "boolean": lambda item: isinstance(item, bool),
            "array": lambda item: isinstance(item, list),
            "object": lambda item: isinstance(item, dict),
            "null": lambda item: item is None,
        }
        if (
            isinstance(expected, str)
            and expected in type_checks
            and not type_checks[expected](value)
        ):
            return False
        if isinstance(expected, list) and not any(
            type_checks[item](value) for item in expected if item in type_checks
        ):
            return False
        if isinstance(value, list):
            if len(value) < int(schema.get("minItems", 0)):
                return False
            if "maxItems" in schema and len(value) > int(schema["maxItems"]):
                return False
            item_schema = schema.get("items")
            if item_schema and any(
                not DFMService._factor_value_matches_schema(item, item_schema)
                for item in value
            ):
                return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                return False
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                return False
            if "maximum" in schema and value > schema["maximum"]:
                return False
            if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
                return False
        return True

    @classmethod
    def _observation_factor_name(cls, observation: Any) -> str:
        """Resolve an Observation to a Factor runtime key without guessing values."""

        provenance = observation.provenance or {}
        raw = (
            provenance.get("factor_runtime_key")
            or provenance.get("runtime_key")
            or provenance.get("factor_id")
            or observation.kind
        )
        return cls._canonical_fact_name(str(raw or ""))

    def _resolve_factor_observations(
        self, manifest: ProjectManifest
    ) -> ProjectManifest:
        """Apply published source_policy before an Observation becomes a Fact."""

        definitions = self.ontology_store.factor_definitions(
            manifest.process or self.config.default_process
        )
        if not definitions:
            return manifest
        by_key = {}
        for definition in definitions:
            by_key[self._canonical_fact_name(definition["runtime_key"])] = definition
            by_key[self._canonical_fact_name(definition["concept_id"])] = definition

        observations = list(manifest.observations)
        facts = list(manifest.facts)
        clarifications = list(manifest.clarifications)
        changed = False
        needs_confirmation: dict[str, str] = {}
        candidates: dict[str, list[tuple[int, Any, str, list[str]]]] = {}

        for index, observation in enumerate(observations):
            if observation.status != "candidate":
                continue
            definition = by_key.get(self._observation_factor_name(observation))
            if definition is None:
                continue
            name = self._canonical_fact_name(definition["runtime_key"])
            policy = definition["source_policy"]
            provenance = observation.provenance or {}
            source = str(
                provenance.get("source") or provenance.get("source_code") or ""
            )
            allowed_sources = set(policy["allowed_sources"])
            aliased_source = self._FACT_SOURCE_ALIASES.get(source, source)
            policy_source = source if source in allowed_sources else aliased_source
            evidence_refs = list(
                dict.fromkeys([
                    *observation.source_refs,
                    *(
                        provenance.get("evidence_refs", [])
                        if isinstance(provenance.get("evidence_refs", []), list)
                        else []
                    ),
                ])
            )
            status = None
            if policy_source not in allowed_sources:
                status = "rejected_source"
            elif not self._factor_value_matches_schema(
                observation.value, definition["data_schema"]
            ):
                status = "rejected_schema"
            elif policy_source in {
                "drawing_recognition",
                "geometry_recognition",
                "DWG",
                "GEO",
            } and (
                policy["min_confidence"] is not None
                and observation.confidence < policy["min_confidence"]
            ):
                status = "rejected_confidence"
            elif (
                policy["evidence_required"]
                and policy_source not in {"user", "USR"}
                and not evidence_refs
            ):
                status = "rejected_evidence"
            elif policy_source in policy["auto_accept_sources"]:
                candidates.setdefault(name, []).append((
                    index,
                    observation,
                    source,
                    evidence_refs,
                ))
            else:
                status = "needs_confirmation"
                needs_confirmation[name] = definition["question"]
            if status is not None:
                observations[index] = replace(observation, status=status)
                changed = True

        for name, items in candidates.items():
            values = {
                json.dumps(item[1].value, ensure_ascii=False, sort_keys=True)
                for item in items
            }
            confirmed = [
                item
                for item in facts
                if item.name == name
                and item.check_id is None
                and item.status == "confirmed"
            ]
            confirmed_values = {
                json.dumps(item.value, ensure_ascii=False, sort_keys=True)
                for item in confirmed
            }
            if len(values | confirmed_values) > 1:
                for index, observation, _source, _evidence in items:
                    observations[index] = replace(observation, status="conflict")
                facts = [
                    replace(item, status="conflict")
                    if item.name == name
                    and item.check_id is None
                    and item.status == "confirmed"
                    else item
                    for item in facts
                ]
                definition = by_key[name]
                needs_confirmation[name] = definition["question"]
                changed = True
                continue
            if not confirmed:
                _index, observation, source, evidence_refs = items[0]
                facts.append(
                    FactRecord(
                        f"fact_{uuid4().hex[:16]}",
                        name,
                        observation.value,
                        source,
                        "confirmed",
                        unit=observation.unit,
                        evidence_refs=evidence_refs,
                    )
                )
            for index, observation, _source, _evidence in items:
                observations[index] = replace(observation, status="accepted")
            changed = True

        for name, question in needs_confirmation.items():
            clarification_id = f"clarification_{name}"
            existing_index = next(
                (
                    index
                    for index, item in enumerate(clarifications)
                    if item.clarification_id == clarification_id
                ),
                None,
            )
            if existing_index is None:
                clarifications.append(
                    ClarificationRecord(clarification_id, question, "open")
                )
            else:
                clarifications[existing_index] = replace(
                    clarifications[existing_index], status="open", answer=None
                )
            changed = True

        if not changed:
            return manifest
        return replace(
            manifest,
            observations=observations,
            facts=facts,
            clarifications=clarifications,
            updated_at=_utc_now(),
        )

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
            if not isinstance(operand, GeometricRuleOperand)
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
        available_pages = sorted({
            int(item["page"])
            for item in fragments
            if isinstance(item.get("page"), int) and item["page"] > 0
        })
        if page_number is not None:
            fragments = [item for item in fragments if item.get("page") == page_number]
        truncated = page_number is None and len(fragments) > 200
        if page_number is None:
            fragments = fragments[:200]
        return {
            "ok": True,
            "project_id": project_id,
            "revision": manifest.revision,
            "input": input_record.to_dict(),
            "fragment_artifact": artifact.to_dict(),
            "page": page_number,
            "available_pages": available_pages,
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
                        "source_type": "DWG",
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
        scope_ids = (
            set(reason.removeprefix("forming_scope:").split(","))
            if reason.startswith("forming_scope:")
            else set()
        )
        feature_ids = (
            set(reason.removeprefix("feature:").split(","))
            if reason.startswith("feature:")
            else set()
        )
        plans = []
        for plan in current.plans:
            if plan.phase != "analysis" or plan.status == "invalidated":
                plans.append(plan)
                continue
            if scope_ids:
                affected = any(
                    any(
                        request.context.get("forming_scope_id") in scope_ids
                        for request in item.items
                    )
                    for item in plan.active_plan_fragments()
                )
                if not affected:
                    plans.append(plan)
                    continue
            elif feature_ids:
                affected = any(
                    any(
                        request.feature_id in feature_ids
                        for request in item.items
                    )
                    or any(dependency["target_feature_id"] in feature_ids for dependency in item.reference_dependencies)
                    for item in plan.active_plan_fragments()
                )
                if plan.plan_fragments and not affected:
                    plans.append(plan)
                    continue
            plans.append(
                replace(
                    plan,
                    status="invalidated",
                    invalidated_by=reason,
                    affected_operation_ids=[
                        item.operation_id for item in plan.operations
                    ],
                )
            )
        return plans

    @staticmethod
    def _invalidate_findings_for_semantics(
        current: ProjectManifest, reason: str
    ):
        scope_ids = (
            set(reason.removeprefix("forming_scope:").split(","))
            if reason.startswith("forming_scope:")
            else set()
        )
        feature_ids = (
            set(reason.removeprefix("feature:").split(","))
            if reason.startswith("feature:")
            else set()
        )
        affected_requests = {
            (request.feature_id, geometric_id)
            for plan in current.plans
            for fragment in plan.plan_fragments
            if any(
                request.context.get("forming_scope_id") in scope_ids
                for request in fragment.items
            )
            for request in fragment.items
            for geometric_id in request.geometric_ids
        }
        affected_requests.update(
            (dependency["feature_id"], dependency["geometric_id"])
            for plan in current.plans for fragment in plan.plan_fragments
            for dependency in fragment.reference_dependencies
            if dependency["target_feature_id"] in feature_ids
        )
        reference_requests_by_fragment = {
            fragment.fragment_id: {(dependency["feature_id"], dependency["geometric_id"])
                for dependency in fragment.reference_dependencies if dependency["target_feature_id"] in feature_ids}
            for plan in current.plans for fragment in plan.plan_fragments
        }

        def affected(item: GeometricFindingRecord) -> bool:
            if not scope_ids and not feature_ids:
                return True
            if feature_ids.intersection(item.feature_refs):
                return True
            if feature_ids and item.plan_fragment_refs:
                requests = {key for ref in item.plan_fragment_refs for key in reference_requests_by_fragment.get(ref, set())}
                if any(feature_id in item.feature_refs and geometric_id in item.geometric_ids for feature_id, geometric_id in requests):
                    return True
                if not scope_ids:
                    return False
            if not scope_ids:
                return any(feature_id in item.feature_refs and geometric_id in item.geometric_ids
                           for feature_id, geometric_id in affected_requests)
            finding_scope_ids = {
                str(context.get("forming_scope_id"))
                for context in item.contexts
                if context.get("forming_scope_id")
            }
            if finding_scope_ids:
                return bool(finding_scope_ids.intersection(scope_ids))
            # Older persisted findings have no context.  Keep the conservative
            # Feature/G fallback until they are regenerated by a current Run.
            return any(
                feature_id in item.feature_refs
                and geometric_id in item.geometric_ids
                for feature_id, geometric_id in affected_requests
            )

        return [
            replace(
                item,
                status="stale",
                validity_status="stale",
                stale_reason=reason,
            )
            if isinstance(item, GeometricFindingRecord)
            and affected(item)
            else item
            for item in current.findings
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
                findings=self._invalidate_findings_for_semantics(
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
            and item.provenance.get("source_type") == "DWG"
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

    def _persist_geometry_candidates(self, project_id: str, *, force: bool = False) -> ProjectManifest:
        store = self._store(project_id)
        current = store.load()
        if geometry_backend_analyzer_key(self.config.geometry_backend) != "occt_cpp":
            return current
        capability = self._ensure_occt_geometry_capability(project_id)
        refreshed = self.discovery.refresh_candidates(
            current, project_dir=self.workspace.project_dir(project_id), **({"force": True} if force else {})
        )
        geometry = dict(refreshed.capabilities.get("geometry_discovery") or {})
        geometry["annotation_kinds"] = list(capability.get("annotation_kinds", []))
        refreshed = replace(refreshed, capabilities={**refreshed.capabilities, "geometry_discovery": geometry})
        if (
            refreshed.features == current.features
            and refreshed.regions == current.regions
            and refreshed.artifacts == current.artifacts
            and refreshed.capabilities == current.capabilities
        ):
            return self._sync_confirmed_main_direction(project_id, current)
        persisted = store.update(
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

        for input_record in self._active_inputs(persisted):
            metadata = persisted.capabilities.get("geometry_discovery", {}).get("inputs", {}).get(input_record.sha256, {})
            raw_scopes = metadata.get("forming_scopes", [])
            if not raw_scopes:
                continue
            persisted, _snapshot = self._persist_forming_snapshot(
                project_id, input_record.sha256,
                {"snapshot_id": metadata["worker_snapshot_id"], "forming_scopes": raw_scopes,
                 "annotations": metadata.get("annotations", []),
                 "relationships": metadata.get("relationships", []),
                 "coverage": metadata.get("coverage", [])},
                reviewed_scope_ids={str(scope["forming_scope_id"]) for scope in raw_scopes if scope.get("status") != "stale"},
                confirmed=False,
            )
        return self._sync_confirmed_main_direction(project_id, persisted)

    def _sync_confirmed_main_direction(self, project_id: str, manifest: ProjectManifest) -> ProjectManifest:
        """Carry the user's confirmed global direction into the native scope once.

        MAIN is an execution scope, not another structure for users to confirm.
        Only synchronize an unchanged, user-supplied direction; local scopes still
        require their own range, structure and direction reviews.
        """
        fact = next((item for item in reversed(manifest.facts)
            if item.status == "confirmed" and item.check_id is None and item.feature_id is None
            and self._canonical_fact_name(item.name) == "pull_dir"), None)
        snapshot = self._latest_discovery_snapshot(manifest)
        if fact is None or snapshot is None:
            return manifest
        direction = fact.value
        if (not isinstance(direction, list) or len(direction) != 3
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in direction) or not 1e-12 < math.hypot(*direction) < math.inf):
            return manifest
        norm = math.hypot(*direction)
        scopes = []
        for scope in snapshot.forming_scopes:
            candidate = scope.get("draw_direction")
            if (scope.get("forming_scope_id") != "MAIN" or scope.get("mode") != "main" or scope.get("status") != "candidate"
                or scope.get("direction_source") != "user_confirmed" or not isinstance(candidate, list)
                or len(candidate) != 3):
                continue
            candidate_norm = math.hypot(*candidate)
            if candidate_norm <= 1e-12 or any(not math.isclose(a / norm, b / candidate_norm, abs_tol=1e-9)
                                            for a, b in zip(direction, candidate)):
                continue
            scopes.append({key: scope[key] for key in (
                "forming_scope_id", "kind", "mode", "draw_direction", "coordinate_system",
                "direction_source", "geometry_refs")})
        if scopes:
            self._revise_forming_scopes(project_id, scopes, manifest.revision, snapshot.snapshot_id)
            return self._store(project_id).load()
        return manifest

    def _structure_review_groups(
        self,
        manifest: ProjectManifest,
        snapshot,
    ) -> list[ReviewGroupRecord]:
        groups = structure_review_groups(
            [feature for feature in manifest.features if feature.feature_id in snapshot.feature_refs],
            manifest.review_groups, snapshot_id=snapshot.snapshot_id, created_at=_utc_now(),
        )
        return annotation_review_groups(snapshot.annotations, groups,
            snapshot_id=snapshot.snapshot_id, created_at=_utc_now())

    def _open_committed_geometry(self, project_id, manifest, input_record):
        project_dir = self.workspace.project_dir(project_id)
        snapshot_id = manifest.capabilities.get("geometry_discovery", {}).get("inputs", {}).get(
            input_record.sha256, {}).get("worker_snapshot_id")
        opened = self.geometry_sessions.open_model(
            project_id, input_path=(project_dir / input_record.relative_path).resolve(),
            input_sha256=input_record.sha256,
            artifact_dir=project_dir / "artifacts" / "geometry_sessions" / input_record.sha256[:16],
            lease_seconds=self.config.geometry_session_lease_seconds,
            expected_snapshot_id=str(snapshot_id) if snapshot_id else None,
        )
        if snapshot_id:
            return self.geometry_sessions.select_snapshot(project_id, input_record.sha256, str(snapshot_id))
        return opened

    def _review_edit_identity(self, manifest, expected_snapshot_id):
        if expected_snapshot_id is None:
            return None
        snapshot = self._latest_discovery_snapshot(manifest)
        if not isinstance(expected_snapshot_id, str) or not snapshot or snapshot.snapshot_id != expected_snapshot_id:
            raise DFMError("geometry_edit_conflict", "The reviewed snapshot changed; reload the current state.",
                {"expected_snapshot_id": expected_snapshot_id, "current_snapshot_id": snapshot.snapshot_id if snapshot else None,
                 "next_action": "refresh_review"})
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError("input_selection_invalid", "Review editing requires one active STEP input.")
        return inputs[0].sha256, geometry_identity(manifest, inputs[0].sha256)

    def _prepare_geometry_edit(self, project_id, manifest, input_sha256, expected_revision, method, params, *, expected_identity=None):
        edit = GeometryEdit.begin(self._store(project_id), expected_revision=expected_revision,
                                  input_sha256=input_sha256, method=method, params=params,
                                  expected_identity=expected_identity)
        try:
            input_record = next(item for item in edit.manifest.inputs if item.sha256 == input_sha256)
            opened = self._open_committed_geometry(project_id, edit.manifest, input_record)
            live_params = {**params, "snapshot_id": self.geometry_sessions.resolve_snapshot_id(project_id, params["snapshot_id"])}
            result = self.geometry_sessions.call(project_id, method, live_params)
            if "job_id" in result:
                result = self.geometry_sessions.wait_job(project_id, str(result["job_id"]))
            edit.assert_current()
            result = self.geometry_sessions.update_snapshot(project_id, input_sha256, opened, result, adopt=False)
            return edit, result
        except Exception as exc:
            if isinstance(exc, DFMError) and exc.code == "snapshot_conflict":
                conflict = DFMError("geometry_edit_conflict", "A concurrent geometry edit changed the prepared branch.")
                self._fail_geometry_edit(project_id, edit, conflict)
                raise conflict from exc
            self._fail_geometry_edit(project_id, edit, exc)
            raise

    def _adopt_committed_geometry(self, project_id, input_sha256):
        try:
            manifest = self._store(project_id).load()
            input_record = next(item for item in manifest.inputs if item.sha256 == input_sha256)
            self._open_committed_geometry(project_id, manifest, input_record)
        except (DFMError, OSError):
            # The committed projection and replay operations are already durable.
            # A dead live handle is recovered on the next use, not a lost edit.
            _LOG.warning("DFM committed geometry requires live-session recovery: %s", project_id, exc_info=True)

    def _fail_geometry_edit(self, project_id, edit, error):
        try:
            edit.fail(error)
        except (DFMError, OSError):
            _LOG.warning("DFM pending edit retained for recovery: %s", edit.edit_id, exc_info=True)
        self._adopt_committed_geometry(project_id, edit.input_sha256)

    def _update_discovery_feature_geometry(
        self,
        project_id: str,
        feature_id: object,
        geometry_refs: object,
        expected_revision: object,
        expected_snapshot_id: object = None,
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise DFMError(
                "manifest_revision_required",
                "Feature editing requires the current project revision.",
            )
        if not isinstance(geometry_refs, list) or not geometry_refs:
            raise DFMError(
                "feature_geometry_invalid",
                "A feature revision must contain geometry_refs.",
            )
        if any(
            not isinstance(value, dict)
            or value.get("kind") != "face"
            or isinstance(value.get("index"), bool)
            or not isinstance(value.get("index"), int)
            or value["index"] < 1
            for value in geometry_refs
        ):
            raise DFMError(
                "feature_geometry_invalid",
                "Feature geometry_refs must be face references from the active snapshot.",
            )
        normalized_refs = sorted(
            (dict(value) for value in geometry_refs),
            key=lambda value: (str(value["kind"]), int(value["index"])),
        )
        store = self._store(project_id)
        manifest = store.load()
        review_identity = self._review_edit_identity(manifest, expected_snapshot_id)
        feature = next(
            (item for item in manifest.features if item.feature_id == feature_id), None
        )
        if feature is None:
            raise DFMError(
                "feature_missing", "The editable Discovery feature was not found."
            )
        if review_identity is None and expected_revision != manifest.revision:
            raise DFMError("manifest_conflict", "Reload the current geometry before editing.",
                           {"expected": expected_revision, "actual": manifest.revision})
        metadata = (
            manifest.capabilities
            .get("geometry_discovery", {})
            .get("inputs", {})
            .get(feature.input_sha256, {})
        )
        snapshot_id = str(metadata.get("worker_snapshot_id") or "")
        if not snapshot_id:
            raise DFMError(
                "geometry_snapshot_missing",
                "The editable feature is not bound to a live Worker snapshot.",
            )
        prior_refs = sorted(feature.properties.get("geometry_refs", []), key=lambda ref: (ref["kind"], ref["index"]))
        if prior_refs == normalized_refs:
            return {"ok": True, "project_id": project_id, "feature_id": feature.feature_id,
                    "geometry_refs": prior_refs, "snapshot_id": snapshot_id, "revision": manifest.revision, "unchanged": True}
        edit, revised = self._prepare_geometry_edit(project_id, manifest, feature.input_sha256,
            expected_revision, "features.revise", {"snapshot_id": snapshot_id, "revisions": [{
                "feature_id": feature.feature_id, "geometry_refs": normalized_refs, "source": "user_confirmed"}]},
            expected_identity=review_identity[1] if review_identity else None)
        try:
            updated_manifest, snapshot = self._persist_forming_snapshot(
                project_id, feature.input_sha256, revised, reviewed_scope_ids=set(), confirmed=False,
                edit=edit, feature_edit_ids={feature.feature_id})
        except Exception as exc:
            self._fail_geometry_edit(project_id, edit, exc)
            raise
        self._adopt_committed_geometry(project_id, feature.input_sha256)
        updated_feature = next(item for item in updated_manifest.features if item.feature_id == feature_id)
        authoritative_refs = list(updated_feature.properties.get("geometry_refs", []))
        viewer_path = materialize_discovery_viewer_manifest(
            self.workspace.project_dir(project_id),
            updated_manifest,
            review_status="pending",
        )
        return {
            "ok": True,
            "project_id": project_id,
            "feature_id": feature.feature_id,
            "geometry_refs": authoritative_refs,
            "snapshot_id": revised["snapshot_id"],
            "discovery_snapshot_id": snapshot.snapshot_id,
            "revision": updated_manifest.revision,
            "viewer_manifest": str(viewer_path) if viewer_path else None,
        }

    def _revise_annotations(self, project_id, annotations, retire_ids, expected_revision, expected_snapshot_id=None):
        manifest = self._store(project_id).load()
        review_identity = self._review_edit_identity(manifest, expected_snapshot_id)
        snapshot = self._latest_discovery_snapshot(manifest)
        if snapshot is None or not snapshot.worker_snapshot_id:
            raise DFMError("discovery_required", "Discover the model before editing surface attributes.")
        if (not isinstance(annotations, list) or any(not isinstance(row, dict) for row in annotations)
            or not isinstance(retire_ids, list) or any(not isinstance(identity, str) for identity in retire_ids)
            or (not annotations and not retire_ids)):
            raise DFMError("annotation_revision_invalid", "Supply annotation changes or retired IDs.")
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError("input_selection_invalid", "Attribute editing requires one active STEP model.")
        input_record = inputs[0]
        self._ensure_occt_geometry_capability(project_id)
        edit, result = self._prepare_geometry_edit(project_id, manifest, input_record.sha256, expected_revision,
            "annotations.revise", {"snapshot_id": snapshot.worker_snapshot_id,
                                   "annotations": annotations, "retire_annotation_ids": retire_ids},
            expected_identity=review_identity[1] if review_identity else None)
        try:
            self._persist_forming_snapshot(project_id, input_record.sha256, result,
                reviewed_scope_ids=set(), confirmed=False, edit=edit)
        except Exception as exc:
            self._fail_geometry_edit(project_id, edit, exc)
            raise
        self._adopt_committed_geometry(project_id, input_record.sha256)
        # No current G reads these attributes. Do not replan unrelated geometry.
        return self._review_context(project_id)

    @staticmethod
    def _scope_content_sha256(scope: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                scope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _forming_scope_review_payload(scope: FormingScopeRecord) -> dict[str, Any]:
        return {
            "forming_scope_id": scope.forming_scope_id,
            "kind": scope.kind,
            "mode": scope.mode,
            "draw_direction": (
                list(scope.draw_direction)
                if scope.draw_direction is not None
                else None
            ),
            "coordinate_system": scope.coordinate_system,
            "direction_source": scope.direction_source,
            "geometry_refs": sorted(
                (item.to_dict() for item in scope.geometry_refs),
                key=lambda item: (
                    item["kind"],
                    item["index"],
                    item["input_sha256"],
                    item["topology_snapshot_id"],
                    item["entity_id"],
                ),
            ),
        }

    @staticmethod
    def _forming_scope_record(
        raw: dict[str, Any], snapshot_id: str
    ) -> FormingScopeRecord:
        raw_direction = raw.get("draw_direction")
        record = FormingScopeRecord(
            forming_scope_id=str(raw.get("forming_scope_id") or ""),
            snapshot_id=snapshot_id,
            mode=str(raw.get("mode") or ""),
            kind=str(raw.get("kind") or ""),
            draw_direction=(
                [float(value) for value in raw_direction]
                if isinstance(raw_direction, list)
                else None
            ),
            coordinate_system=str(raw.get("coordinate_system") or ""),
            direction_source=(
                str(raw["direction_source"])
                if raw.get("direction_source") is not None
                else None
            ),
            status=str(raw.get("status") or "candidate"),
            geometry_refs=[
                GeometryRef.from_dict(dict(value))
                for value in raw.get("geometry_refs", [])
                if isinstance(value, dict)
            ],
            diagnostics=dict(raw.get("diagnostics") or {}),
            supersedes_scope_ids=list(raw.get("supersedes_scope_ids") or []),
        )
        record.validate()
        return record

    def _persist_forming_snapshot(
        self,
        project_id: str,
        input_sha256: str,
        result: dict[str, Any],
        *,
        reviewed_scope_ids: set[str],
        confirmed: bool,
        edit: GeometryEdit | None = None,
        edited_scope_ids: set[str] | None = None,
        feature_edit_ids: set[str] | None = None,
        main_direction: list[float] | None = None,
        staged_direction_ids: set[str] | None = None,
    ) -> tuple[ProjectManifest, Any]:
        snapshot_id = str(result.get("snapshot_id") or "")
        raw_scopes = result.get("forming_scopes")
        if not snapshot_id or not isinstance(raw_scopes, list):
            raise DFMError(
                "geometry_protocol_invalid",
                "A forming-scope response omitted its immutable snapshot state.",
            )
        scopes = [
            self._forming_scope_record(dict(item), snapshot_id)
            for item in raw_scopes
            if isinstance(item, dict)
        ]
        if reviewed_scope_ids.difference(item.forming_scope_id for item in scopes):
            raise DFMError(
                "geometry_protocol_invalid",
                "The Worker omitted a requested forming scope from its result.",
            )
        store = self._store(project_id)
        edited_scope_ids = set(edited_scope_ids or [])
        feature_edit_ids = set(feature_edit_ids or [])
        staged_direction_ids = set(staged_direction_ids or [])
        direction_fact = FactRecord(f"fact_{uuid4().hex[:16]}", "pull_dir", main_direction, "user", "confirmed") if main_direction is not None else None

        def apply(current: ProjectManifest) -> ProjectManifest:
            facts = [*current.facts, direction_fact] if direction_fact is not None else current.facts
            capabilities = dict(current.capabilities)
            geometry_discovery = dict(capabilities.get("geometry_discovery") or {})
            inputs = dict(geometry_discovery.get("inputs") or {})
            if self.occt_geometry_capability is not None:
                geometry_discovery["annotation_kinds"] = list(self.occt_geometry_capability.get("annotation_kinds", []))
            inputs[input_sha256] = {
                **dict(inputs.get(input_sha256) or {}),
                "worker_snapshot_id": snapshot_id,
                **{name: list(result.get(name, [])) for name in (
                    "annotations", "relationships", "forming_scopes", "coverage"
                )},
            }
            if direction_fact is not None:
                inputs[input_sha256]["discovery_context_sha256"] = discovery_context_identity(replace(current, facts=facts))
            geometry_discovery["inputs"] = inputs
            capabilities["geometry_discovery"] = geometry_discovery

            history = list(current.forming_scopes)
            known = {(item.forming_scope_id, item.snapshot_id) for item in history}
            history.extend(
                item
                for item in scopes
                if (item.forming_scope_id, item.snapshot_id) not in known
            )
            review_groups = list(current.review_groups)
            if edited_scope_ids or feature_edit_ids:
                review_groups = [replace(item, status="stale")
                    if item.status in {"pending_review", "confirmed"} and (
                        item.kind in {"undercut_scope", "local_structure", "local_direction"}
                        and (edited_scope_ids | feature_edit_ids).intersection(item.target_refs)
                    ) else item for item in review_groups]
            for scope in scopes:
                if scope.forming_scope_id not in reviewed_scope_ids:
                    continue
                if scope.status == "stale":
                    continue
                scope_payload = self._forming_scope_review_payload(scope)
                digest = self._scope_content_sha256(scope_payload)
                group_kind = (
                    "product_forming"
                    if scope.mode == "main"
                    else "local_direction"
                    if confirmed or scope.forming_scope_id in staged_direction_ids
                    else "undercut_scope"
                )
                review_groups = [
                    replace(item, status="stale")
                    if item.kind == group_kind
                    and scope.forming_scope_id in item.target_refs
                    and item.status in {"pending_review", "confirmed"}
                    and item.content_sha256 != digest
                    else item
                    for item in review_groups
                ]
                if confirmed:
                    review_groups = [
                        replace(
                            item,
                            status="confirmed",
                            confirmed_at=_utc_now(),
                        )
                        if item.kind == group_kind
                        and item.status == "pending_review"
                        and item.content_sha256 == digest
                        and scope.forming_scope_id in item.target_refs
                        else item
                        for item in review_groups
                    ]
                if any(
                    item.kind == group_kind
                    and scope.forming_scope_id in item.target_refs
                    and item.content_sha256 == digest
                    and item.status
                    == ("confirmed" if confirmed else "pending_review")
                    for item in review_groups
                ):
                    continue
                status = "confirmed" if confirmed else "pending_review"
                review_groups.append(
                    ReviewGroupRecord(
                        review_group_id=(
                            f"review.forming.{scope.forming_scope_id}.{digest[:16]}"
                        ),
                        kind=group_kind,
                        target_refs=[scope.forming_scope_id],
                        content_sha256=digest,
                        source="user",
                        status=status,
                        created_at=_utc_now(),
                        confirmed_at=_utc_now() if confirmed else None,
                        snapshot_id=snapshot_id,
                    )
                )
            plans = (
                self._invalidate_plans_for_semantics(
                    current, f"forming_scope:{','.join(sorted(reviewed_scope_ids))}"
                )
                if confirmed or edited_scope_ids or staged_direction_ids
                else current.plans
            )
            newly_confirmed_targets = {
                target
                for item in review_groups
                if item.status == "confirmed"
                and item.kind in {"product_forming", "local_structure"}
                and item.confirmed_at is not None
                for target in item.target_refs
            }
            if feature_edit_ids:
                review = dict(capabilities.get("discovery_review") or {})
                review["edited_feature_ids"] = sorted(set(review.get("edited_feature_ids", [])) | feature_edit_ids)
                capabilities["discovery_review"] = review
                plans = self._invalidate_plans_for_semantics(current, "feature:" + ",".join(sorted(feature_edit_ids)))
            features = current.features
            if "features" in result:
                input_record = next(item for item in current.inputs if item.sha256 == input_sha256)
                projection = self.discovery.geometry_provider.project_snapshot(input_record, result)
                features = reconcile_worker_features(features, projection.features, input_sha256)
            return replace(
                current,
                facts=facts,
                features=[
                    replace(item, status="confirmed")
                    if item.feature_id in newly_confirmed_targets
                    else item
                    for item in features
                ],
                forming_scopes=history,
                review_groups=review_groups,
                plans=plans,
                findings=(
                    self._invalidate_findings_for_semantics(
                        current,
                        f"forming_scope:{','.join(sorted(reviewed_scope_ids))}",
                    )
                    if confirmed or edited_scope_ids or staged_direction_ids
                    else current.findings
                ),
                capabilities=capabilities,
                updated_at=_utc_now(),
            )

        def prepare(current):
            updated = apply(current)
            frozen, discovery_snapshot = self.discovery.freeze(updated)
            groups = self._structure_review_groups(frozen, discovery_snapshot)
            if feature_edit_ids:
                affected_local_scopes = {
                    scope.forming_scope_id for scope in scopes if scope.mode == "local" and any(
                        group.kind == "local_structure" and scope.forming_scope_id in group.target_refs
                        and feature_edit_ids.intersection(group.target_refs)
                        for group in current.review_groups
                    )
                }
                affected_local_scopes.update(
                    str(row["to_ref"]) for row in result.get("relationships", [])
                    if row.get("from_ref") in feature_edit_ids
                    and row.get("type") in {"intersects_forming_scope", "contained_in_forming_scope"}
                    and any(group.kind == "local_structure" and row["to_ref"] in group.target_refs
                            for group in current.review_groups)
                )
                groups = local_structure_review_groups(
                    scopes, frozen.features, list(result.get("relationships", [])), groups,
                    scope_ids=affected_local_scopes, snapshot_id=discovery_snapshot.snapshot_id, created_at=_utc_now(),
                )
            capabilities = dict(frozen.capabilities)
            review = dict(capabilities.get("discovery_review") or {})
            if review or feature_edit_ids:
                pending = any(item.kind == "ordinary_structure" and item.status == "pending_review" for item in groups)
                review.update({"snapshot_id": discovery_snapshot.snapshot_id, "status": "pending" if pending else "confirmed"})
                capabilities["discovery_review"] = review
            if feature_edit_ids:
                frozen = replace(frozen, findings=self._invalidate_findings_for_semantics(frozen, "feature:" + ",".join(sorted(feature_edit_ids))))
            return replace(frozen, capabilities=capabilities, review_groups=groups)

        persisted = edit.commit(snapshot_id, prepare) if edit is not None else store.update(prepare)
        discovery_snapshot = self._latest_discovery_snapshot(persisted)
        return persisted, discovery_snapshot

    def _supplement_local_scopes(
        self,
        project_id: str,
        scope_ids: set[str],
    ) -> tuple[ProjectManifest, Any]:
        store = self._store(project_id)
        manifest = store.load()
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError(
                "input_selection_invalid",
                "Scoped feature discovery requires exactly one active STEP input.",
            )
        input_record = inputs[0]
        project_dir = self.workspace.project_dir(project_id)
        latest_scopes = {
            item.forming_scope_id: item for item in manifest.forming_scopes
        }
        for scope_id in sorted(scope_ids):
            scope = latest_scopes.get(scope_id)
            if scope is None or scope.mode != "local":
                raise DFMError(
                    "forming_scope_invalid",
                    "Local supplemental discovery requires a reviewed local scope.",
                    {"forming_scope_id": scope_id},
                )
            metadata = (
                manifest.capabilities
                .get("geometry_discovery", {})
                .get("inputs", {})
                .get(input_record.sha256, {})
            )
            logical_snapshot_id = str(metadata.get("worker_snapshot_id") or "")
            projection = self.discovery.geometry_provider.recognize_scope(
                    input_record,
                    snapshot_id=logical_snapshot_id,
                    forming_scope_id=scope_id,
                    project_dir=project_dir,
            )
            coverage = list(projection.diagnostics["coverage"])
            worker_snapshot_id = projection.worker_snapshot_id
            merged_features = reconcile_worker_features(
                manifest.features, projection.features, input_record.sha256
            )
            scoped_feature_ids = sorted(
                {str(item["from_ref"]) for item in projection.relationships
                 if item.get("to_ref") == scope_id and item.get("type") in {
                     "intersects_forming_scope", "contained_in_forming_scope"
                 }}
            )
            content = local_structure_review_content(scope, merged_features, projection.relationships)
            digest = self._scope_content_sha256(content)
            structure_group = ReviewGroupRecord(
                review_group_id=f"review.local-structure.{scope_id}.{digest[:16]}",
                kind="local_structure",
                target_refs=[scope_id, *scoped_feature_ids],
                content_sha256=digest,
                source="user",
                status="pending_review",
                created_at=_utc_now(),
                snapshot_id=worker_snapshot_id,
            )
            direction_payload = self._forming_scope_review_payload(scope)
            direction_digest = self._scope_content_sha256(direction_payload)
            direction_group = ReviewGroupRecord(
                review_group_id=(
                    f"review.local-direction.{scope_id}.{direction_digest[:16]}"
                ),
                kind="local_direction",
                target_refs=[scope_id],
                content_sha256=direction_digest,
                source="user",
                status="pending_review",
                created_at=_utc_now(),
                snapshot_id=worker_snapshot_id,
            )

            def apply(current: ProjectManifest) -> ProjectManifest:
                capabilities = dict(current.capabilities)
                geometry = dict(capabilities.get("geometry_discovery") or {})
                by_input = dict(geometry.get("inputs") or {})
                input_state = dict(by_input.get(input_record.sha256) or {})
                prior_coverage = [
                    dict(item)
                    for item in input_state.get("coverage", [])
                    if not (
                        isinstance(item, dict)
                        and item.get("context", {}).get("forming_scope_id")
                        == scope_id
                    )
                ]
                input_state.update({
                    "worker_snapshot_id": worker_snapshot_id,
                    "coverage": coverage,
                    "annotations": projection.annotations,
                    "relationships": projection.relationships,
                    "forming_scopes": projection.forming_scopes,
                })
                by_input[input_record.sha256] = input_state
                geometry["inputs"] = by_input
                capabilities["geometry_discovery"] = geometry
                review_groups = list(current.review_groups)
                for new_group in (structure_group, direction_group):
                    review_groups = [
                        replace(item, status="stale")
                        if item.kind == new_group.kind
                        and scope_id in item.target_refs
                        and item.status in {"pending_review", "confirmed"}
                        and item.content_sha256 != new_group.content_sha256
                        else item
                        for item in review_groups
                    ]
                    if not any(
                        item.kind == new_group.kind
                        and scope_id in item.target_refs
                        and item.content_sha256 == new_group.content_sha256
                        and item.status in {"pending_review", "confirmed"}
                        for item in review_groups
                    ):
                        review_groups.append(new_group)
                invalidation_reason = f"forming_scope:{scope_id}"
                return replace(
                    current,
                    features=merged_features,
                    review_groups=review_groups,
                    plans=self._invalidate_plans_for_semantics(
                        current, invalidation_reason
                    ),
                    findings=self._invalidate_findings_for_semantics(
                        current, invalidation_reason
                    ),
                    capabilities=capabilities,
                    updated_at=_utc_now(),
                )

            manifest = store.update(apply, expected_revision=manifest.revision)
        frozen, snapshot = self.discovery.freeze(manifest)

        def persist(current: ProjectManifest) -> ProjectManifest:
            capabilities = dict(current.capabilities)
            review = dict(capabilities.get("discovery_review") or {})
            if review.get("status") == "confirmed":
                review["snapshot_id"] = snapshot.snapshot_id
                capabilities["discovery_review"] = review
            return replace(
                current,
                discovery_snapshots=frozen.discovery_snapshots,
                capabilities=capabilities,
                updated_at=_utc_now(),
            )

        persisted = store.update(persist, expected_revision=manifest.revision)
        return persisted, snapshot

    def _analyze_forming_scope(
        self,
        project_id: str,
        forming_scope_id: object,
        mode: object,
        draw_direction: object,
    ) -> dict[str, Any]:
        scope_mode = str(mode or "main").strip()
        scope_id = str(
            forming_scope_id or ("MAIN" if scope_mode == "main" else "")
        ).strip()
        if not scope_id or scope_mode not in {"main", "local"}:
            raise DFMError(
                "forming_scope_invalid",
                "Forming-scope analysis requires an ID and main/local mode.",
            )
        manifest = self._store(project_id).load()
        if draw_direction is None and scope_mode == "main":
            draw_direction = next(
                (
                    fact.value
                    for fact in reversed(manifest.facts)
                    if fact.status == "confirmed"
                    and fact.check_id is None
                    and fact.feature_id is None
                    and self._canonical_fact_name(fact.name) == "pull_dir"
                ),
                None,
            )
        if (
            not isinstance(draw_direction, list)
            or len(draw_direction) != 3
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in draw_direction
            )
            or not 1e-12 < math.hypot(*draw_direction) < math.inf
        ):
            raise DFMError(
                "direction_invalid",
                "A forming-scope direction must contain three model-coordinate numbers.",
            )
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError(
                "input_selection_invalid",
                "Forming-scope analysis requires exactly one active STEP input.",
            )
        input_record = inputs[0]
        project_dir = self.workspace.project_dir(project_id)
        opened = self._open_committed_geometry(project_id, manifest, input_record)
        metadata = (
            manifest.capabilities
            .get("geometry_discovery", {})
            .get("inputs", {})
            .get(input_record.sha256, {})
        )
        logical_snapshot_id = str(
            metadata.get("worker_snapshot_id") or opened["snapshot_id"]
        )
        snapshot_id = self.geometry_sessions.resolve_snapshot_id(
            project_id, logical_snapshot_id
        )
        submitted = self.geometry_sessions.call(
            project_id,
            "forming_scopes.analyze",
            {
                "snapshot_id": snapshot_id,
                "forming_scope_id": scope_id,
                "mode": scope_mode,
                "draw_direction": list(draw_direction),
                "coordinate_system": "model",
                "direction_source": "user_confirmed",
            },
        )
        result = self.geometry_sessions.wait_job(project_id, str(submitted["job_id"]))
        result = self.geometry_sessions.update_snapshot(
            project_id, input_record.sha256, opened, result
        )
        analyzed_scope_ids = {
            str(item.get("forming_scope_id") or "")
            for item in result.get("coverage", [])
            if isinstance(item, dict) and item.get("forming_scope_id")
        } or {scope_id}
        persisted, discovery_snapshot = self._persist_forming_snapshot(
            project_id,
            input_record.sha256,
            result,
            reviewed_scope_ids=analyzed_scope_ids,
            confirmed=False,
        )
        selected = next(
            item
            for item in persisted.forming_scopes
            if item.forming_scope_id == scope_id
            and item.snapshot_id == result["snapshot_id"]
        )
        return {
            "ok": True,
            "project_id": project_id,
            "status": "forming_scope_review_required",
            "forming_scope": selected.to_dict(),
            "forming_scopes": [
                item.to_dict()
                for item in persisted.forming_scopes
                if item.snapshot_id == result["snapshot_id"]
                and item.forming_scope_id in analyzed_scope_ids
            ],
            "review_groups": [
                item.to_dict()
                for item in persisted.review_groups
                if item.status == "pending_review"
                and any(value in item.target_refs for value in analyzed_scope_ids)
            ],
            "coverage": result.get("coverage", []),
            "snapshot": discovery_snapshot.to_dict(),
            "revision": persisted.revision,
            "requires_user_response": True,
            "next_action": (
                "confirm_discovery" if scope_mode == "local"
                else "revise_forming_scopes"
            ),
        }

    def _revise_scope_candidates(self, project_id, raw_scopes, retire_scope_ids, expected_revision, expected_snapshot_id=None):
        manifest = self._store(project_id).load()
        review_identity = self._review_edit_identity(manifest, expected_snapshot_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise DFMError("manifest_revision_required", "Range editing requires the current project revision.")
        if review_identity is None and expected_revision != manifest.revision:
            raise DFMError("manifest_conflict", "Reload the current geometry before editing.")
        snapshot = self._latest_discovery_snapshot(manifest)
        if snapshot is None or not snapshot.worker_snapshot_id:
            raise DFMError("discovery_required", "Discover the model before editing a forming scope.")
        prior = {str(scope["forming_scope_id"]): scope for scope in snapshot.forming_scopes}
        if not isinstance(raw_scopes, list) or not isinstance(retire_scope_ids, list):
            raise DFMError("forming_scope_invalid", "Scope replacements and retire_scope_ids must be arrays.")
        if any(not isinstance(identity, str) or identity not in prior or prior[identity]["mode"] != "local"
               or prior[identity]["status"] == "stale" for identity in retire_scope_ids) or len(set(retire_scope_ids)) != len(retire_scope_ids):
            raise DFMError("forming_scope_invalid", "Only active local scopes can be explicitly retired.")
        scopes = []
        for raw in raw_scopes:
            if not isinstance(raw, dict) or set(raw) - {"forming_scope_id", "kind", "mode", "geometry_refs", "supersedes_scope_ids",
                                                      "draw_direction", "direction_source", "coordinate_system", "status"}:
                raise DFMError("forming_scope_invalid", "Candidate scope edits contain undeclared fields.")
            if (raw.get("draw_direction") is not None or raw.get("direction_source") is not None
                or raw.get("coordinate_system", "model") != "model" or raw.get("status", "candidate") != "candidate"):
                raise DFMError("forming_scope_invalid", "Range editing cannot confirm a direction.")
            identity = str(raw.get("forming_scope_id") or "scope_" + uuid4().hex[:16])
            previous = prior.get(identity, {})
            if previous and (previous["mode"] != "local" or previous["status"] == "stale"):
                raise DFMError("forming_scope_invalid", "The main or retired scope cannot be edited as a local range.")
            scope = {"forming_scope_id": identity, "kind": raw.get("kind", previous.get("kind", "undercut")),
                     "mode": raw.get("mode", "local"), "geometry_refs": raw.get("geometry_refs"),
                     "draw_direction": None, "coordinate_system": "model", "direction_source": None, "status": "candidate"}
            if scope["mode"] != "local" or scope["kind"] not in {"undercut", "local_forming"}:
                raise DFMError("forming_scope_invalid", "Range editing accepts local scopes only.")
            if not isinstance(scope["geometry_refs"], list) or not scope["geometry_refs"]:
                raise DFMError("forming_scope_invalid", "A local range requires nonempty geometry_refs.")
            if not previous and retire_scope_ids:
                scope["supersedes_scope_ids"] = raw.get("supersedes_scope_ids", retire_scope_ids)
            elif "supersedes_scope_ids" in raw:
                scope["supersedes_scope_ids"] = raw["supersedes_scope_ids"]
            scopes.append(scope)
        if not scopes and not retire_scope_ids:
            raise DFMError("forming_scope_invalid", "At least one range edit or retirement is required.")
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError("input_selection_invalid", "Range editing requires one active STEP model.")
        input_record = inputs[0]
        # Saving an unchanged range must not create a new semantic branch or
        # clear its staged/confirmed direction and dependent review groups.
        def unchanged(raw, scope):
            previous = prior.get(scope["forming_scope_id"])
            return (previous is not None
                and set(raw) <= {"forming_scope_id", "kind", "mode", "geometry_refs"}
                and previous["kind"] == scope["kind"] and previous["mode"] == scope["mode"]
                and all(isinstance(ref, dict) for ref in scope["geometry_refs"])
                and sorted(json.dumps(ref, sort_keys=True) for ref in previous["geometry_refs"])
                    == sorted(json.dumps(ref, sort_keys=True) for ref in scope["geometry_refs"]))
        if (not retire_scope_ids and len({scope["forming_scope_id"] for scope in scopes}) == len(scopes)
            and all(unchanged(raw, scope) for raw, scope in zip(raw_scopes, scopes))):
            return {"ok": True, "project_id": project_id, "revision": manifest.revision,
                    "snapshot": snapshot.to_dict(), "forming_scopes": snapshot.forming_scopes,
                    "unchanged": True}
        params = {"snapshot_id": snapshot.worker_snapshot_id, "scopes": scopes, "retire_scope_ids": retire_scope_ids}
        edit, result = self._prepare_geometry_edit(project_id, manifest, input_record.sha256, expected_revision,
                                                   "forming_scopes.revise", params,
                                                   expected_identity=review_identity[1] if review_identity else None)
        affected = {scope["forming_scope_id"] for scope in scopes} | set(retire_scope_ids)
        try:
            persisted, frozen = self._persist_forming_snapshot(project_id, input_record.sha256, result,
                reviewed_scope_ids=affected, confirmed=False, edit=edit, edited_scope_ids=affected)
        except Exception as exc:
            self._fail_geometry_edit(project_id, edit, exc)
            raise
        self._adopt_committed_geometry(project_id, input_record.sha256)
        viewer = materialize_discovery_viewer_manifest(self.workspace.project_dir(project_id), persisted, review_status="pending")
        return {"ok": True, "project_id": project_id, "status": "forming_scope_review_required",
                "revision": persisted.revision, "snapshot": frozen.to_dict(), "forming_scopes": frozen.forming_scopes,
                "review_groups": [group.to_dict() for group in persisted.review_groups if group.status == "pending_review"],
                "viewer_manifest": str(viewer) if viewer else None, "next_action": "confirm_discovery"}

    def _update_forming_scope_geometry(self, project_id, scope_id, geometry_refs, expected_revision, expected_snapshot_id=None):
        if not isinstance(scope_id, str) or not scope_id:
            raise DFMError("forming_scope_invalid", "A forming_scope_id is required.")
        return self._revise_scope_candidates(project_id, [{"forming_scope_id": scope_id, "geometry_refs": geometry_refs}],
                                             [], expected_revision, expected_snapshot_id)

    def _stage_forming_direction(self, project_id, scope, expected_revision, expected_snapshot_id):
        manifest = self._store(project_id).load()
        review_identity = self._review_edit_identity(manifest, expected_snapshot_id)
        snapshot = self._latest_discovery_snapshot(manifest)
        original = next((item for item in snapshot.forming_scopes if item["forming_scope_id"] == scope["forming_scope_id"]
                         and item["mode"] == "local" and item.get("status") != "stale"), None) if snapshot else None
        direction = scope.get("draw_direction")
        if (original is None or not isinstance(direction, list) or len(direction) != 3
            or any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) for value in direction)
            or not 1e-12 < math.hypot(*direction) < math.inf):
            raise DFMError("direction_invalid", "A candidate direction requires a current local scope and a finite nonzero vector.")
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError("input_selection_invalid", "Direction editing requires one active STEP input.")
        payload = {key: original[key] for key in ("forming_scope_id", "kind", "mode", "geometry_refs")}
        payload.update(draw_direction=direction, direction_source="user_confirmed", coordinate_system="model", status="candidate")
        edit, result = self._prepare_geometry_edit(project_id, manifest, inputs[0].sha256, expected_revision,
            "forming_scopes.revise", {"snapshot_id": snapshot.worker_snapshot_id, "scopes": [payload]},
            expected_identity=review_identity[1] if review_identity else None)
        try:
            persisted, frozen = self._persist_forming_snapshot(project_id, inputs[0].sha256, result,
                reviewed_scope_ids={payload["forming_scope_id"]}, confirmed=False, edit=edit,
                staged_direction_ids={payload["forming_scope_id"]})
        except Exception as exc:
            self._fail_geometry_edit(project_id, edit, exc)
            raise
        self._adopt_committed_geometry(project_id, inputs[0].sha256)
        viewer = materialize_discovery_viewer_manifest(self.workspace.project_dir(project_id), persisted, review_status="pending")
        return {"ok": True, "project_id": project_id, "status": "forming_direction_staged", "revision": persisted.revision,
                "snapshot": frozen.to_dict(), "viewer_manifest": str(viewer) if viewer else None}

    def _revise_forming_scopes(
        self, project_id: str, raw_scopes: object, expected_revision: object = None,
        expected_snapshot_id: object = None,
    ) -> dict[str, Any]:
        if not isinstance(raw_scopes, list) or not raw_scopes:
            raise DFMError(
                "forming_scope_invalid",
                "At least one user-confirmed forming scope is required.",
            )
        scopes = [dict(item) for item in raw_scopes if isinstance(item, dict)]
        if len(scopes) != len(raw_scopes):
            raise DFMError("forming_scope_invalid", "Forming scopes must be objects.")
        allowed = {
            "forming_scope_id",
            "kind",
            "mode",
            "draw_direction",
            "coordinate_system",
            "direction_source",
            "geometry_refs",
        }
        if any(set(item) != allowed for item in scopes):
            raise DFMError(
                "forming_scope_invalid",
                "A confirmed forming scope contains undeclared fields.",
            )
        manifest = self._store(project_id).load()
        review_identity = self._review_edit_identity(manifest, expected_snapshot_id)
        if expected_revision is not None and (isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int) or (review_identity is None and expected_revision != manifest.revision)):
            raise DFMError("manifest_conflict", "The project changed; reload the review before submitting.",
                           {"expected": expected_revision, "actual": manifest.revision})
        for scope in scopes:
            direction = scope["draw_direction"]
            if (not isinstance(direction, list) or len(direction) != 3
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in direction)
                or not 1e-12 < math.hypot(*direction) < math.inf):
                raise DFMError("direction_invalid", "A confirmed direction must contain three finite, non-zero model-coordinate numbers.")
            scope_id = str(scope["forming_scope_id"])
            mode = str(scope["mode"])
            expected_kind = (
                "product_forming" if mode == "main" else "local_direction"
            )
            if not any(
                item.kind == expected_kind
                and scope_id in item.target_refs
                and item.status in {"pending_review", "confirmed"}
                for item in manifest.review_groups
            ):
                raise DFMError(
                    "review_group_pending",
                    "The forming scope must complete its current review group first.",
                    {
                        "forming_scope_id": scope_id,
                        "review_group_kind": expected_kind,
                    },
                )
            if mode == "local" and not any(
                item.kind == "undercut_scope"
                and scope_id in item.target_refs
                and item.status == "confirmed"
                for item in manifest.review_groups
            ):
                raise DFMError(
                    "review_group_pending",
                    "The undercut range must be confirmed before local direction review.",
                    {"forming_scope_id": scope_id},
                )
            if mode == "local" and not any(
                item.kind == "local_structure"
                and scope_id in item.target_refs
                and item.status == "confirmed"
                for item in manifest.review_groups
            ):
                raise DFMError(
                    "review_group_pending",
                    "The local structure must be confirmed before local direction review.",
                    {"forming_scope_id": scope_id},
                )
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError(
                "input_selection_invalid",
                "Forming-scope revision requires exactly one active STEP input.",
            )
        input_record = inputs[0]
        metadata = (
            manifest.capabilities
            .get("geometry_discovery", {})
            .get("inputs", {})
            .get(input_record.sha256, {})
        )
        logical_snapshot_id = str(metadata.get("worker_snapshot_id") or "")
        if not logical_snapshot_id:
            raise DFMError(
                "geometry_snapshot_missing",
                "Forming-scope revision requires a live Discovery snapshot.",
            )
        current_snapshot = self._latest_discovery_snapshot(manifest)
        previous = {item["forming_scope_id"]: item for item in current_snapshot.forming_scopes} if current_snapshot else {}
        changed_main = [scope for scope in scopes if scope["mode"] == "main"
                        and previous.get(scope["forming_scope_id"], {}).get("draw_direction") != scope["draw_direction"]]
        if changed_main:
            if len(scopes) != 1:
                raise DFMError("forming_scope_invalid", "Change the main direction separately from local scopes.")
            scope = changed_main[0]
            edit, result = self._prepare_geometry_edit(project_id, manifest, input_record.sha256,
                manifest.revision if expected_revision is None else expected_revision, "forming_scopes.analyze",
                {"snapshot_id": logical_snapshot_id, "forming_scope_id": scope["forming_scope_id"], "mode": "main",
                 "draw_direction": scope["draw_direction"], "coordinate_system": "model", "direction_source": "user_confirmed"},
                expected_identity=review_identity[1] if review_identity else None)
            try:
                scope_ids = {str(item["forming_scope_id"]) for item in result["forming_scopes"]}
                persisted, snapshot = self._persist_forming_snapshot(project_id, input_record.sha256, result,
                    reviewed_scope_ids=scope_ids, confirmed=False, edit=edit, edited_scope_ids=scope_ids,
                    main_direction=list(scope["draw_direction"]))
            except Exception as exc:
                self._fail_geometry_edit(project_id, edit, exc)
                raise
            self._adopt_committed_geometry(project_id, input_record.sha256)
            return self._revise_forming_scopes(project_id, [scope], persisted.revision, snapshot.snapshot_id)
        edit, result = self._prepare_geometry_edit(project_id, manifest, input_record.sha256,
            manifest.revision if expected_revision is None else expected_revision, "forming_scopes.revise",
            {"snapshot_id": logical_snapshot_id, "scopes": [{**scope, "status": "confirmed"} for scope in scopes]},
            expected_identity=review_identity[1] if review_identity else None)
        scope_ids = {str(item["forming_scope_id"]) for item in scopes}
        try:
            persisted, discovery_snapshot = self._persist_forming_snapshot(
                project_id, input_record.sha256, result, reviewed_scope_ids=scope_ids, confirmed=True, edit=edit)
        except Exception as exc:
            self._fail_geometry_edit(project_id, edit, exc)
            raise
        self._adopt_committed_geometry(project_id, input_record.sha256)
        pending = any(item.status == "pending_review" for item in persisted.review_groups)
        viewer = materialize_discovery_viewer_manifest(self.workspace.project_dir(project_id), persisted,
                                                       review_status="pending" if pending else "confirmed")
        confirmed_scopes = [
            item.to_dict()
            for item in persisted.forming_scopes
            if item.snapshot_id == result["snapshot_id"]
            and item.forming_scope_id in scope_ids
        ]
        return {
            "ok": True,
            "project_id": project_id,
            "status": "forming_scopes_confirmed",
            "forming_scopes": confirmed_scopes,
            "snapshot": discovery_snapshot.to_dict(),
            "revision": persisted.revision,
            "viewer_manifest": str(viewer) if viewer else None,
            "requires_user_response": False,
            "next_action": "plan",
        }

    def _confirm_discovery(
        self,
        project_id: str,
        expected_revision: object,
        raw_review_group_ids: object = None,
        expected_snapshot_id: object = None,
    ) -> dict[str, Any]:
        if geometry_backend_analyzer_key(self.config.geometry_backend) == "occt_cpp":
            self._ensure_occt_geometry_capability(project_id)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
        ):
            raise DFMError(
                "manifest_revision_required",
                "Discovery confirmation received an invalid project revision.",
            )
        store = self._store(project_id)
        manifest = store.load()
        review_identity = self._review_edit_identity(manifest, expected_snapshot_id)
        revision = (
            expected_revision
            if isinstance(expected_revision, int)
            else manifest.revision
        )
        review = manifest.capabilities.get("discovery_review", {})
        if raw_review_group_ids is not None and (
            not isinstance(raw_review_group_ids, list)
            or any(not isinstance(item, str) or not item for item in raw_review_group_ids)
            or len(raw_review_group_ids) != len(set(raw_review_group_ids))
        ):
            raise DFMError(
                "review_group_invalid",
                "Discovery confirmation requires unique review_group_ids.",
            )
        requested_group_ids = set(raw_review_group_ids or [])
        discovered, snapshot = self.discovery.freeze(manifest)
        discovered = replace(discovered, review_groups=self._structure_review_groups(discovered, snapshot))
        pending_groups = {
            item.review_group_id: item
            for item in discovered.review_groups
            if item.status == "pending_review"
        }
        if not requested_group_ids:
            requested_group_ids = {
                item.review_group_id
                for item in pending_groups.values()
                if item.kind == "ordinary_structure"
            }
            if not requested_group_ids:
                independently_confirmable = [
                    item.review_group_id
                    for item in pending_groups.values()
                    if item.kind in {"undercut_scope", "local_structure"}
                ]
                if len(independently_confirmable) == 1:
                    requested_group_ids = set(independently_confirmable)
                elif len(independently_confirmable) > 1:
                    raise DFMError(
                        "review_group_required",
                        "Independent local review groups require explicit review_group_ids.",
                        {"review_group_ids": sorted(independently_confirmable)},
                    )
        if not requested_group_ids:
            raise DFMError(
                "discovery_review_closed", "Discovery is not awaiting confirmation."
            )
        missing_groups = requested_group_ids.difference(pending_groups)
        if missing_groups:
            raise DFMError(
                "review_group_stale",
                "A selected review group is missing, stale, or already closed.",
                {"review_group_ids": sorted(missing_groups)},
            )
        direction_groups = sorted(
            group_id
            for group_id in requested_group_ids
            if pending_groups[group_id].kind == "local_direction"
        )
        if direction_groups:
            raise DFMError(
                "review_group_invalid",
                "Local direction groups must be confirmed through forming_scopes.revise.",
                {"review_group_ids": direction_groups},
            )
        product_groups = {identity for identity in requested_group_ids
                          if pending_groups[identity].kind == "product_forming"}
        if product_groups:
            scope_ids = {target for identity in product_groups for target in pending_groups[identity].target_refs}
            selected_scopes = [{key: value for key, value in scope.items() if key in {
                "forming_scope_id", "kind", "mode", "draw_direction", "coordinate_system", "direction_source", "geometry_refs"
            }} for scope in snapshot.forming_scopes if scope["forming_scope_id"] in scope_ids]
            result = self._revise_forming_scopes(project_id, selected_scopes, revision, expected_snapshot_id)
            remaining = requested_group_ids - product_groups
            if remaining:
                return self._confirm_discovery(project_id, result["revision"], sorted(remaining),
                    result["snapshot"]["snapshot_id"] if review_identity else None)
            context = self._review_context(project_id)
            return {**context, "status": "forming_scopes_confirmed", "next_action": "plan"}
        confirmed_at = _utc_now()
        confirmed_targets = {
            target
            for group_id in requested_group_ids
            if pending_groups[group_id].kind != "surface_attribute"
            for target in pending_groups[group_id].target_refs
        }
        confirmed_groups = [
            replace(
                item,
                status="confirmed",
                confirmed_at=confirmed_at,
            )
            if item.review_group_id in requested_group_ids
            else item
            for item in discovered.review_groups
        ]
        def apply(current: ProjectManifest) -> ProjectManifest:
            source = discovered
            groups_to_save = confirmed_groups
            if review_identity:
                if geometry_identity(current, review_identity[0]) != review_identity[1]:
                    raise DFMError("geometry_edit_conflict", "The reviewed geometry changed before confirmation.")
                live_groups = {item.review_group_id: item for item in current.review_groups}
                if any(identity not in live_groups or live_groups[identity].status != "pending_review"
                       or live_groups[identity].content_sha256 != pending_groups[identity].content_sha256
                       for identity in requested_group_ids):
                    raise DFMError("review_group_stale", "A selected review group changed before confirmation.")
                source = current
                groups_to_save = [replace(item, status="confirmed", confirmed_at=confirmed_at)
                                  if item.review_group_id in requested_group_ids else item
                                  for item in source.review_groups]
            pending_structure = any(item.kind == "ordinary_structure" and item.status == "pending_review"
                                    for item in groups_to_save)
            capabilities = dict(source.capabilities)
            capabilities["discovery_review"] = {
                **dict(source.capabilities.get("discovery_review", review)),
                "status": (
                    "pending" if pending_structure else "confirmed"
                ),
                "snapshot_id": snapshot.snapshot_id,
                "confirmed_at": (
                    None if pending_structure else confirmed_at
                ),
            }
            return replace(
                current,
                features=[
                    replace(item, status="confirmed")
                    if item.feature_id in confirmed_targets
                    else item
                    for item in source.features
                ],
                regions=source.regions,
                observations=source.observations,
                fusion_links=source.fusion_links,
                discovery_snapshots=source.discovery_snapshots,
                artifacts=source.artifacts,
                review_groups=groups_to_save,
                capabilities=capabilities,
                updated_at=_utc_now(),
            )

        confirmed = store.update(apply, expected_revision=revision if review_identity is None else None)
        confirmed_undercut_scope_ids = {
            target
            for group_id in requested_group_ids
            if pending_groups[group_id].kind == "undercut_scope"
            for target in pending_groups[group_id].target_refs
        }
        if confirmed_undercut_scope_ids:
            confirmed, snapshot = self._supplement_local_scopes(
                project_id, confirmed_undercut_scope_ids
            )
        confirmed = self._ensure_clarifications(project_id, phase="analysis")
        remaining_structure_groups = [item for item in confirmed.review_groups
                                      if item.kind == "ordinary_structure" and item.status == "pending_review"]
        open_clarifications = self._open_clarifications(confirmed, phase="analysis")
        pending_local_structure_groups = [
            item
            for item in confirmed.review_groups
            if item.kind == "local_structure" and item.status == "pending_review"
        ]
        pending_local_direction_groups = [
            item
            for item in confirmed.review_groups
            if item.kind == "local_direction" and item.status == "pending_review"
        ]
        pending_review = bool(
            remaining_structure_groups
            or pending_local_structure_groups
            or pending_local_direction_groups
        )
        viewer_path = materialize_discovery_viewer_manifest(
            self.workspace.project_dir(project_id),
            confirmed,
            review_status="pending" if pending_review else "confirmed",
        )
        return {
            "ok": True,
            "project_id": project_id,
            "status": (
                "discovery_review_required"
                if remaining_structure_groups or pending_local_structure_groups
                else "forming_scope_review_required"
                if pending_local_direction_groups
                else "discovery_confirmed"
            ),
            "phase": "discovery",
            "snapshot": snapshot.to_dict(),
            "annotations": snapshot.annotations,
            "relationships": snapshot.relationships,
            "forming_scopes": snapshot.forming_scopes,
            "revision": confirmed.revision,
            "viewer_manifest": str(viewer_path) if viewer_path else None,
            "review_groups": [
                item.to_dict()
                for item in confirmed.review_groups
                if item.status == "pending_review"
            ],
            "requires_user_response": bool(
                remaining_structure_groups
                or pending_local_structure_groups
                or pending_local_direction_groups
                or open_clarifications
            ),
            "next_action": (
                "confirm_discovery"
                if remaining_structure_groups
                else "confirm_discovery"
                if pending_local_structure_groups
                else "revise_forming_scopes"
                if pending_local_direction_groups
                else "clarify"
                if open_clarifications
                else "plan"
            ),
            "open_clarifications": [item.to_dict() for item in open_clarifications],
        }

    def _discovery_response(
        self,
        manifest: ProjectManifest,
        snapshot,
        discovery_plan: PlanRecord,
        **control: Any,
    ) -> dict[str, Any]:
        result = {
            "ok": True,
            "project_id": manifest.project_id,
            "phase": "discovery",
            "annotations": snapshot.annotations,
            "relationships": snapshot.relationships,
            "forming_scopes": snapshot.forming_scopes,
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
            "capability": self.discovery.capability(manifest.project_id),
            "review_groups": [item.to_dict() for item in manifest.review_groups if item.status != "stale"],
            "drawing_discovery": manifest.capabilities.get("drawing_discovery", {}),
            "open_clarifications": [
                item.to_dict()
                for item in self._open_clarifications(manifest, phase="analysis")
            ],
        }
        result.update(control)
        return result

    def _review_context(self, project_id: str, *, manifest: ProjectManifest | None = None) -> dict[str, Any]:
        """Read current reviewable state without starting algorithms or changing it."""
        manifest = manifest if manifest is not None else self._store(project_id).load()
        snapshot = self._latest_discovery_snapshot(manifest)
        if snapshot is None:
            raise DFMError("discovery_required", "Feature discovery must finish before review.")
        active = set(snapshot.feature_refs)
        return {
            "ok": True, "project_id": project_id, "revision": manifest.revision,
            "edit_timeout_ms": (self.config.geometry_timeout_seconds + 60) * 1000,
            "snapshot_id": snapshot.snapshot_id, "worker_snapshot_id": snapshot.worker_snapshot_id,
            "features": [item.to_dict() for item in manifest.features if item.feature_id in active],
            "annotations": snapshot.annotations, "relationships": snapshot.relationships,
            "annotation_kinds": manifest.capabilities.get("geometry_discovery", {}).get("annotation_kinds", []),
            "forming_scopes": snapshot.forming_scopes,
            "review_groups": [item.to_dict() for item in self._structure_review_groups(manifest, snapshot)
                              if item.status != "stale"],
        }

    def _prepare_review(self, project_id, expected_revision, expected_snapshot_id):
        manifest = self._store(project_id).load()
        # Preparation reads the latest state before the user starts editing.
        # Unlike a save, it must not reject a known older review of this model.
        latest = self._latest_discovery_snapshot(manifest)
        if expected_snapshot_id is not None:
            reviewed = next((row for row in manifest.discovery_snapshots if row.snapshot_id == expected_snapshot_id), None)
            geometry_ids = {item.input_id for item in manifest.inputs if item.kind in {"step", "parasolid"}}
            if reviewed is None or latest is None:
                raise DFMError("geometry_edit_conflict", "Reload the current review before editing.")
            if ({key: value for key, value in reviewed.input_hashes.items() if key in geometry_ids}
                != {key: value for key, value in latest.input_hashes.items() if key in geometry_ids}):
                raise DFMError("geometry_model_changed", "The model changed; reopen its current 3D view before editing.")
        if expected_snapshot_id is None and expected_revision != manifest.revision:
            raise DFMError("manifest_conflict", "Reload the current review before editing.")
        inputs = [item for item in self._active_inputs(manifest) if item.kind == "step"]
        if len(inputs) != 1:
            raise DFMError("input_selection_invalid", "Review editing requires one active STEP input.")
        restored = False
        try:
            self._open_committed_geometry(project_id, manifest, inputs[0])
        except DFMError as exc:
            if exc.code not in {"geometry_snapshot_stale", "geometry_recovery_dependency_changed", "geometry_recovery_unavailable"}:
                raise
            # Regeneration may replace detector output, never silently discard a
            # committed human revision whose replay log cannot be restored.
            if any(edit.get("status") == "committed" and not (
                edit.get("method") == "forming_scopes.revise" and edit.get("params", {}).get("scopes")
                and all(scope.get("mode") == "main" for scope in edit["params"]["scopes"]))
                for edit in manifest.geometry_edits):
                raise DFMError("geometry_recovery_review_required",
                    "This project contains saved geometry edits. Its recovery log must be restored before editing.",
                    {"cause": exc.code, "next_action": "restore_recovery_log"}) from exc
            directory = self.workspace.project_dir(project_id) / "artifacts" / "geometry_sessions" / inputs[0].sha256[:16]
            self.geometry_sessions.restart_discovery(project_id, inputs[0].sha256, directory)
            manifest = self._persist_geometry_candidates(project_id, force=True)
            restored = True
        manifest = self._store(project_id).load()
        if {(item.input_id, item.sha256) for item in self._active_inputs(manifest) if item.kind == "step"} != {(item.input_id, item.sha256) for item in inputs}:
            raise DFMError("geometry_model_changed", "The model changed while preparing review; reopen its current 3D view.")
        context = self._review_context(project_id, manifest=manifest)
        viewer = materialize_discovery_viewer_manifest(self.workspace.project_dir(project_id), manifest,
            review_status="pending" if any(group.status == "pending_review" for group in manifest.review_groups) else "confirmed")
        return {**context, "session_restored": restored, "review_refreshed": expected_snapshot_id != context["snapshot_id"],
                "viewer_manifest": str(viewer) if viewer else None}

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
                findings=self._invalidate_findings_for_semantics(
                    current, "fusion_review"
                ),
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
                    findings=self._invalidate_findings_for_semantics(
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
                item.name: item
                for item in current.facts
                if item.status == "confirmed" and item.check_id is None
            }
            facts = list(current.facts)
            observations = []
            changed = False
            for observation in current.observations:
                if observation.provenance.get("source_type") != "DWG":
                    observations.append(observation)
                    continue
                policy = policies.get(observation.kind)
                if not policy or "DWG" not in set(policy.get("allowed_sources", [])):
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
                        and "DWG" in set(policy.get("auto_accept_sources", []))
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
                            source="DWG",
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
                        and "DWG"
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
            return geometry_backend_analyzer_key(requested_key)
        geometry_kinds = {
            item.kind
            for item in self._active_inputs(manifest)
            if item.kind in {"step", "parasolid"}
        }
        if "step" in geometry_kinds:
            return self.config.geometry_analyzer_key
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
        confirmed = {
            self._canonical_fact_name(fact.name): fact.value
            for fact in manifest.facts
            if fact.status == "confirmed"
            and fact.check_id is None
            and fact.feature_id is None
        }
        confirmed_by_check = {
            (self._canonical_fact_name(fact.name), fact.check_id): fact.value
            for fact in manifest.facts
            if fact.status == "confirmed"
            and fact.check_id is not None
            and fact.feature_id is None
        }
        confirmed_by_feature = {
            (
                self._canonical_fact_name(fact.name),
                fact.check_id,
                fact.feature_id,
            ): fact.value
            for fact in manifest.facts
            if fact.status == "confirmed" and fact.feature_id is not None
        }
        existing = {item.clarification_id: item for item in manifest.clarifications}
        if phase == "discovery":
            # These three project prerequisites are independent of the
            # published process-specific ontology and precede recognition.
            pending = []
            for name, question, _choices in self._STARTUP_FACTS:
                if name in confirmed:
                    continue
                clarification_id = f"clarification_{name}"
                item = existing.get(clarification_id)
                pending.append(
                    replace(item, question=question, status="open", answer=None)
                    if item is not None
                    else ClarificationRecord(clarification_id, question, "open")
                )
            return pending
        adapter = self.process_registry.get(
            process or manifest.process or self.config.default_process
        )
        requirements = adapter.fact_requirements()
        applicable_check_ids: set[str] | None = None
        if phase == "analysis":
            snapshot = next(
                (
                    item
                    for item in reversed(manifest.discovery_snapshots)
                    if item.status == "frozen"
                ),
                None,
            )
            if snapshot is None:
                return []
            applicable_check_ids = self._applicable_analysis_check_ids(
                manifest, snapshot
            )
        result = []
        scoped_result = []
        for requirement in requirements:
            name = self._canonical_fact_name(requirement.name)
            required_in_phase = requirement.phase == phase
            if phase == "analysis":
                required_in_phase = (
                    name not in self._DISCOVERY_FACT_NAMES
                    and requirement.phase == "analysis"
                    and (
                        not requirement.check_ids
                        or bool(
                            applicable_check_ids.intersection(requirement.check_ids)
                        )
                    )
                )
            if phase != "all" and not required_in_phase:
                continue
            if phase == "analysis" and requirement.impact_scope == "特征实例级":
                feature_refs: list[str] = []
                for target in self._geometric_analysis_scopes(manifest, snapshot):
                    target_checks = {str(target["check_id"])}
                    feature = target.get("feature")
                    feature_id = str(getattr(feature, "feature_id", "") or "")
                    if (
                        feature_id
                        and set(requirement.check_ids).intersection(target_checks)
                        and feature_id not in feature_refs
                    ):
                        feature_refs.append(feature_id)
                for feature_id in feature_refs:
                    if (name, None, feature_id) in confirmed_by_feature:
                        continue
                    clarification_id = self._clarification_id(
                        name, feature_id=feature_id
                    )
                    item = existing.get(clarification_id)
                    question = f"【特征实例：{feature_id}】{requirement.question}"
                    scoped_result.append(
                        replace(item, question=question, status="open", answer=None)
                        if item is not None
                        else ClarificationRecord(clarification_id, question, "open")
                    )
                continue
            if (
                phase == "analysis"
                and requirement.impact_scope != "整件级"
                and name in self._CHECK_SCOPED_FACTS.get(adapter.key, ())
            ):
                for check_id in requirement.check_ids:
                    if check_id not in applicable_check_ids:
                        continue
                    choice_facts = {
                        **confirmed,
                        **{
                            fact_name: value
                            for (
                                fact_name,
                                fact_check_id,
                            ), value in confirmed_by_check.items()
                            if fact_check_id == check_id
                        },
                    }
                    choices = self.ontology_store.fact_check_choices(
                        adapter.key, name, check_id, choice_facts
                    )
                    if (
                        not choices
                        or name in confirmed
                        or (name, check_id) in confirmed_by_check
                    ):
                        continue
                    check_name = self.ontology_store.check_context(check_id)["check"][
                        "name_zh"
                    ]
                    clarification_id = self._clarification_id(name, check_id=check_id)
                    item = existing.get(clarification_id)
                    scope = (
                        f"【影响范围：{requirement.impact_scope}】"
                        if requirement.impact_scope
                        else ""
                    )
                    question = f"{check_name}：{scope}{requirement.question}"
                    scoped_result.append(
                        replace(item, question=question, status="open", answer=None)
                        if item is not None
                        else ClarificationRecord(clarification_id, question, "open")
                    )
                continue
            choices = self.ontology_store.fact_enum_values(adapter.key, name)
            if name in confirmed and (not choices or confirmed[name] in choices):
                continue
            question = requirement.question
            if requirement.impact_scope:
                question = f"【影响范围：{requirement.impact_scope}】{question}"
            clarification_id = self._clarification_id(name)
            item = existing.get(clarification_id)
            result.append(
                replace(item, question=question, status="open", answer=None)
                if item is not None
                else ClarificationRecord(clarification_id, question, "open")
            )
        return [*result, *scoped_result]

    def clarification_choices(self, process: str, fact_name: str) -> tuple[Any, ...]:
        """User-facing options for one pending factor."""

        name, check_id = self._fact_reference(fact_name)
        if check_id:
            if name not in self._CHECK_SCOPED_FACTS.get(process, ()):
                return ()
            return self.ontology_store.fact_check_choices(process, name, check_id)
        if name == "process":
            return tuple(
                getattr(self.process_registry.get(key), "display_name_zh", key)
                for key in self.process_registry.keys()
            )
        for startup_name, _question, options in self._STARTUP_FACTS:
            if name == startup_name:
                return options
        return self.ontology_store.fact_enum_values(process, name)

    def clarification_feature_refs(
        self, project_id: str, fact_name: str
    ) -> tuple[str, ...]:
        """Discovered features whose applicable checks require one factor."""

        manifest = self._store(project_id).load()
        snapshot = next(
            (
                item
                for item in reversed(manifest.discovery_snapshots)
                if item.status == "frozen"
            ),
            None,
        )
        if snapshot is None:
            return ()
        name, check_id, feature_id = self._fact_scope_reference(fact_name)
        if feature_id:
            return (feature_id,)
        check_ids = {check_id} if check_id else set()
        if not check_ids:
            adapter = self.process_registry.get(
                manifest.process or self.config.default_process
            )
            requirement = next(
                (
                    item
                    for item in adapter.fact_requirements()
                    if self._canonical_fact_name(item.name) == name
                ),
                None,
            )
            if requirement is not None:
                check_ids.update(requirement.check_ids)
        if not check_ids:
            return ()

        feature_refs: list[str] = []
        for target in self._geometric_analysis_scopes(manifest, snapshot):
            target_checks = {str(target["check_id"])}
            feature = target.get("feature")
            feature_id = getattr(feature, "feature_id", "")
            if check_ids.intersection(target_checks) and feature_id:
                feature_refs.append(str(feature_id))
        return tuple(dict.fromkeys(feature_refs))

    def clarification_metadata(self, project_id: str, fact_name: str) -> dict[str, Any]:
        """Return UI metadata for one pending ontology-driven question."""

        manifest = self._store(project_id).load()
        process = manifest.process or self.config.default_process
        name, check_id, feature_id = self._fact_scope_reference(fact_name)
        definition = next(
            (
                item
                for item in self.ontology_store.factor_definitions(process)
                if self._canonical_fact_name(item["runtime_key"]) == name
            ),
            None,
        )
        impact_scope = str((definition or {}).get("impact_scope") or "")
        feature_refs = (
            (feature_id,)
            if feature_id
            else self.clarification_feature_refs(project_id, fact_name)
        )
        feature_kind = ""
        if feature_id:
            feature = next(
                (item for item in manifest.features if item.feature_id == feature_id),
                None,
            )
            feature_kind = feature.kind if feature is not None else ""
        return {
            "runtime_key": name,
            "check_id": check_id,
            "feature_id": feature_id,
            "feature_kind": feature_kind,
            "feature_refs": list(feature_refs),
            "impact_scope": impact_scope,
            "has_default": bool((definition or {}).get("has_default")),
            "default_value": (definition or {}).get("default_value"),
        }

    def _reconcile_clarifications(self, project_id: str) -> ProjectManifest:
        """Close old open clarification rows when an alias fact already exists."""
        store = self._store(project_id)

        def reconcile(current: ProjectManifest) -> ProjectManifest:
            confirmed = {
                (
                    self._canonical_fact_name(fact.name),
                    fact.check_id,
                    fact.feature_id,
                ): fact
                for fact in current.facts
                if fact.status == "confirmed"
            }
            changed = False
            rows = []
            for item in current.clarifications:
                name, check_id, feature_id = self._fact_scope_reference(
                    item.clarification_id.removeprefix("clarification_")
                )
                fact = confirmed.get((name, check_id, feature_id))
                if fact is None and check_id and feature_id is None:
                    global_fact = confirmed.get((name, None, None))
                    if (
                        global_fact is not None
                        and global_fact.value
                        in self.clarification_choices(
                            current.process or self.config.default_process,
                            f"{name}@{check_id}",
                        )
                    ):
                        fact = global_fact
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
        discovery_open = self._open_clarifications(manifest, phase="discovery")
        phase = (
            "discovery"
            if discovery_open or not manifest.discovery_snapshots
            else "analysis"
        )
        payload["open_clarifications"] = [
            item.to_dict()
            for item in (
                discovery_open
                if phase == "discovery"
                else self._open_clarifications(manifest, phase=phase)
            )
        ]
        payload["discovery_capability"] = self.discovery.capability(
            manifest.project_id
        )
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

    def _applicable_analysis_check_ids(
        self, manifest: ProjectManifest, snapshot
    ) -> set[str]:
        """Return Checks backed by one discovered Feature and public G capability."""

        return {
            str(scope["check_id"])
            for scope in self._geometric_analysis_scopes(manifest, snapshot)
        }

    def _geometric_analysis_scopes(
        self, manifest: ProjectManifest, snapshot
    ) -> list[dict[str, Any]]:
        """Resolve ontology operands directly to immutable Worker Features and G IDs."""

        specs = self.ontology_store.geometric_scope_specs(
            manifest.process or self.config.default_process
        )
        features = [
            item
            for item in manifest.features
            if item.feature_id in snapshot.feature_refs
            and item.status != "rejected"
        ]
        return [
            {**spec, "feature": feature}
            for spec in specs
            for feature in features
            if feature.kind == spec["feature_kind"]
        ]

    def _bind_geometric_scope(
        self,
        process_plan,
        manifest,
        snapshot,
    ):
        """Bind public G operands to immutable Worker feature identities."""

        if not process_plan.rule_bindings:
            return process_plan
        operands = [
            operand
            for binding in process_plan.rule_bindings
            for operand in binding.measurement_operands()
        ]
        if any(not isinstance(operand, GeometricRuleOperand) for operand in operands):
            raise DFMError(
                "geometric_plan_invalid",
                "A dfm.geometry/v2 plan may contain only public G operands.",
            )
        if self.occt_geometry_capability is None:
            raise DFMError(
                "geometry_capability_missing",
                "Feature + G binding requires a validated dfm.geometry/v2 capability.",
            )
        if operands:
            if not snapshot.worker_snapshot_id:
                raise DFMError(
                    "geometry_snapshot_missing",
                    "The confirmed Discovery snapshot is not bound to a live Worker snapshot.",
                    {"discovery_snapshot_id": snapshot.snapshot_id},
                )
            available = [
                item
                for item in manifest.features
                if item.feature_id in snapshot.feature_refs
                and item.status != "rejected"
            ]
            geometric_capabilities = geometry_binding_index(
                self.occt_geometry_capability or {}
            )
            latest_scopes = {
                str(raw["forming_scope_id"]): self._forming_scope_record(raw, snapshot.worker_snapshot_id)
                for raw in snapshot.forming_scopes
            }
            confirmed_scopes = [
                item
                for item in latest_scopes.values()
                if item.status == "confirmed"
            ]
            waiting_scopes = [
                scope
                for scope in latest_scopes.values()
                if scope.status == "candidate"
            ]

            def geometry_keys(feature):
                return {
                    (
                        str(item.get("kind")),
                        int(item.get("index", 0)),
                        str(item.get("input_sha256") or ""),
                    )
                    for item in feature.properties.get("geometry_refs", [])
                    if isinstance(item, dict)
                }

            def context_choices(geometric_id, feature):
                declaration = geometric_capabilities.get(geometric_id, {})
                required = set(
                    declaration.get("context_schema", {}).get("required", [])
                )
                if not required:
                    return [{}]
                supported_modes = set(declaration.get("direction_modes", []))
                feature_refs = geometry_keys(feature)
                matching = []
                for scope in [*confirmed_scopes, *waiting_scopes]:
                    scope_refs = {
                        (item.kind, item.index, item.input_sha256)
                        for item in scope.geometry_refs
                    }
                    if scope.mode not in supported_modes or (
                        scope_refs and feature_refs.isdisjoint(scope_refs)
                    ):
                        continue
                    if scope.status == "confirmed":
                        matching.append({
                            "forming_scope_id": scope.forming_scope_id,
                            "draw_direction": list(scope.draw_direction),
                            "coordinate_system": "model",
                            "direction_source": "user_confirmed",
                        })
                    else:
                        matching.append({
                            "forming_scope_id": scope.forming_scope_id,
                        })
                # Empty context is a deliberate waiting-input identity.  It is
                # frozen into a non-runnable fragment and never sent to Worker.
                return matching or [{}]

            def candidates(selector):
                feature_kind = str(selector.get("feature_kind") or "")
                return [
                    item
                    for item in available
                    if not feature_kind or item.kind == feature_kind
                ]

            def relationship_refs(feature):
                refs: set[str] = set()

                def collect(value):
                    if isinstance(value, str) and value in snapshot.feature_refs:
                        refs.add(value)
                    elif isinstance(value, dict):
                        for nested in value.values():
                            collect(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            collect(nested)

                collect(feature.relationships)
                return refs

            bound = []
            for binding in process_plan.rule_bindings:
                if not isinstance(binding, GeometricRuleBinding):
                    raise DFMError(
                        "geometric_plan_invalid",
                        "A dfm.geometry/v2 plan may contain only public G bindings.",
                    )
                selector_map = process_plan.binding_selectors.get(
                    binding.binding_id, {}
                )
                primary_candidates = candidates(
                    selector_map.get(binding.operand_alias, {})
                )
                for primary in primary_candidates:
                    selected_additional = []
                    complete = True
                    related = relationship_refs(primary)
                    for operand in binding.additional_operands:
                        operand_candidates = candidates(
                            selector_map.get(operand.alias, {})
                        )
                        same = [
                            item
                            for item in operand_candidates
                            if item.feature_id == primary.feature_id
                        ]
                        linked = [
                            item
                            for item in operand_candidates
                            if item.feature_id in related
                        ]
                        if len(same) == 1:
                            selected = same[0]
                        elif len(linked) == 1:
                            selected = linked[0]
                        elif len(operand_candidates) == 1:
                            selected = operand_candidates[0]
                        elif not operand_candidates:
                            complete = False
                            break
                        else:
                            raise DFMError(
                                "analysis_operand_ambiguous",
                                "A Geometric operand resolves to multiple Worker features.",
                                {
                                    "check_id": binding.check_id,
                                    "operand_alias": operand.alias,
                                    "feature_ids": [
                                        item.feature_id for item in operand_candidates
                                    ],
                                },
                            )
                        selected_additional.append(
                            replace(
                                operand,
                                feature_refs=[selected.feature_id],
                            )
                        )
                    if not complete:
                        continue
                    choices = [
                        context_choices(binding.geometric_id, primary),
                        *[
                            context_choices(
                                operand.geometric_id,
                                next(
                                    item
                                    for item in available
                                    if item.feature_id == operand.feature_refs[0]
                                ),
                            )
                            for operand in selected_additional
                        ],
                    ]
                    for contexts in product(*choices):
                        scope_ids = {
                            str(value.get("forming_scope_id"))
                            for value in contexts
                            if value.get("forming_scope_id")
                        }
                        if len(scope_ids) > 1:
                            continue
                        context_identity = json.dumps(
                            contexts,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        suffix = hashlib.sha256(
                            (primary.feature_id + context_identity).encode("utf-8")
                        ).hexdigest()[:12]
                        bound.append(
                            replace(
                                binding,
                                binding_id=f"{binding.binding_id}.{suffix}",
                                feature_refs=[primary.feature_id],
                                context=dict(contexts[0]),
                                additional_operands=[
                                    replace(operand, context=dict(context))
                                    for operand, context in zip(
                                        selected_additional, contexts[1:]
                                    )
                                ],
                            )
                        )
            return replace(
                process_plan,
                operations=[],
                rule_bindings=bound,
            )

    @staticmethod
    def _confirmed_process(manifest: ProjectManifest) -> str | None:
        return next(
            (
                str(fact.value)
                for fact in reversed(manifest.facts)
                if fact.name == "process" and fact.status == "confirmed"
            ),
            None,
        )

    def _resolve_process(
        self, manifest: ProjectManifest, requested: object = None
    ) -> str:
        requested_value = str(requested or "").strip()
        if requested_value:
            try:
                requested_value = str(
                    self._normalize_fact_value("process", requested_value)
                )
            except DFMError as exc:
                raise DFMError(
                    "process_invalid",
                    exc.message,
                    {"process": requested},
                ) from exc
        confirmed = self._confirmed_process(manifest)
        if confirmed and requested_value and confirmed != requested_value:
            raise DFMError(
                "process_conflict",
                "The requested process conflicts with the confirmed process fact.",
                {"confirmed": confirmed, "requested": requested_value},
            )
        process = (
            confirmed
            or requested_value
            or manifest.process
            or self.config.default_process
        )
        self.process_registry.get(process)
        return process

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
        if not manifest.runs:
            raise DFMError(
                "run_not_found",
                "No DFM run exists for this project; start an analysis run first.",
                {"action": action, "next_action": "start"},
            )
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

    def _report_context(
        self,
        project_id: str,
        run_id: str,
        wait_seconds: object = 0,
    ) -> dict[str, Any]:
        """Return the complete deterministic Runtime when Agent editing can begin."""

        try:
            timeout = float(wait_seconds or 0)
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "wait_invalid",
                "wait_seconds must be a number between 0 and 180.",
            ) from exc
        if timeout < 0 or timeout > 180:
            raise DFMError(
                "wait_invalid",
                "wait_seconds must be a number between 0 and 180.",
            )

        deadline = time.monotonic() + timeout
        while True:
            run = self.jobs.status(project_id, run_id)
            if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "ok": True,
                    "project_id": project_id,
                    "ready": False,
                    "next_action": "report_context",
                    "run": self._report_run_dict(run),
                }
            time.sleep(min(0.25, remaining))

        if run.status is RunStatus.SUCCEEDED:
            html_artifact = next(
                (
                    item
                    for item in reversed(run.artifacts)
                    if item.kind == "report_html"
                ),
                None,
            )
            if html_artifact is not None:
                return {
                    "ok": True,
                    "project_id": project_id,
                    "ready": False,
                    "complete": True,
                    "next_action": "result",
                    "run": self._report_run_dict(run),
                }

        retryable_report = (
            run.status is RunStatus.FAILED
            and any(
                item.kind == "report_html_runtime" for item in run.artifacts
            )
            and any(item.kind == "result_manifest" for item in run.artifacts)
        )
        if run.status in {RunStatus.BLOCKED, RunStatus.CANCELLED} or (
            run.status is RunStatus.FAILED and not retryable_report
        ):
            return {
                "ok": True,
                "project_id": project_id,
                "ready": False,
                "complete": True,
                "next_action": "status",
                "run": self._report_run_dict(run),
            }

        if run.status is not RunStatus.REPORTING and not retryable_report:
            raise DFMError(
                "report_not_available",
                "This DFM run cannot enter Agent report editing.",
                {"run_id": run_id, "status": run.status.value},
            )

        runtime_artifact = next(
            (
                item
                for item in reversed(run.artifacts)
                if item.kind == "report_html_runtime"
            ),
            None,
        )
        if runtime_artifact is None:
            raise DFMError(
                "report_input_missing",
                "This run does not contain the Runtime required by the HTML report.",
                {"run_id": run_id},
            )
        runtime_path = (
            self.workspace.project_dir(project_id) / runtime_artifact.relative_path
        )
        rows = self._read_ndjson(runtime_path)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise DFMError(
                "report_input_invalid",
                "The HTML report Runtime must contain exactly one JSON object.",
                {"path": runtime_artifact.relative_path},
            )
        return {
            "ok": True,
            "project_id": project_id,
            "ready": True,
            "complete": False,
            "next_action": "render_html",
            "required_issue_ids": self._report_issue_ids(rows[0]),
            "runtime": rows[0],
            "run": self._report_run_dict(run),
        }

    def _render_html(
        self,
        project_id: str,
        run_id: str,
        llm_content: object,
        *,
        on_update: Callable[[RunRecord], None] | None = None,
    ) -> dict[str, Any]:
        """Queue HTML rendering after the Agent supplies the narrative contract."""

        if not isinstance(llm_content, dict):
            raise DFMError(
                "report_content_invalid",
                "render_html requires an llm_content object.",
            )
        run = self.jobs.status(project_id, run_id)
        existing_html = next(
            (item for item in reversed(run.artifacts) if item.kind == "report_html"),
            None,
        )
        if run.status is RunStatus.SUCCEEDED and existing_html is not None:
            project_dir = self.workspace.project_dir(project_id)
            return {
                "ok": True,
                "project_id": project_id,
                "run": self._report_run_dict(run),
                "report": {
                    **existing_html.to_dict(),
                    "path": str(project_dir / existing_html.relative_path),
                },
                "accepted": False,
                "complete": True,
                "next_action": "result",
            }
        if run.status not in {
            RunStatus.REPORTING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
        } or (
            run.status is RunStatus.FAILED
            and not any(
                item.kind == "report_html_runtime" for item in run.artifacts
            )
        ):
            raise DFMError(
                "result_not_ready",
                "The deterministic DFM result is not ready for HTML reporting.",
                {"run_id": run_id, "status": run.status.value},
            )
        runtime_artifact = next(
            (
                item
                for item in reversed(run.artifacts)
                if item.kind == "report_html_runtime"
            ),
            None,
        )
        if runtime_artifact is None:
            raise DFMError(
                "report_input_missing",
                "This run does not contain the resources required by the HTML report.",
                {"run_id": run_id},
            )

        project_dir = self.workspace.project_dir(project_id)
        output_dir = project_dir / "runs" / run_id / "artifacts"
        llm_path = output_dir / "llm_content.jsonl"
        html_path = output_dir / "report.html"
        runtime_rows = self._read_ndjson(project_dir / runtime_artifact.relative_path)
        if len(runtime_rows) != 1 or not isinstance(runtime_rows[0], dict):
            raise DFMError(
                "report_input_invalid",
                "The HTML report Runtime must contain exactly one JSON object.",
            )
        required_ids = self._report_issue_ids(runtime_rows[0])
        submitted_issues = llm_content.get("issues")
        if not isinstance(submitted_issues, list) or any(
            not isinstance(issue, dict) or not isinstance(issue.get("issue_id"), str)
            for issue in submitted_issues
        ):
            raise DFMError(
                "report_content_invalid",
                "llm_content.issues must contain issue_id objects.",
                {"required_issue_ids": required_ids},
            )
        submitted_ids = [issue["issue_id"] for issue in submitted_issues]
        if len(submitted_ids) != len(set(submitted_ids)) or set(submitted_ids) != set(
            required_ids
        ):
            raise DFMError(
                "report_content_invalid",
                "llm_content issue IDs must exactly match report_context.required_issue_ids.",
                {
                    "required_issue_ids": required_ids,
                    "missing_issue_ids": sorted(set(required_ids) - set(submitted_ids)),
                    "unknown_issue_ids": sorted(set(submitted_ids) - set(required_ids)),
                },
            )
        try:
            llm_content = normalize_editorial_content(llm_content)
        except ContractError as exc:
            raise DFMError("report_content_invalid", str(exc)) from exc
        try:
            llm_payload = (
                json.dumps(llm_content, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
        except (TypeError, ValueError) as exc:
            raise DFMError(
                "report_content_invalid",
                "The Agent-authored HTML report content is not JSON serializable.",
                {"error": str(exc)},
            ) from exc

        def render(
            update_progress: Callable[[str, int], None],
            cancellation: CancellationToken,
        ) -> list[ArtifactRecord]:
            suffix = uuid4().hex
            llm_candidate = llm_path.with_name(f".{llm_path.name}.{suffix}.tmp")
            html_candidate = html_path.with_name(
                f".{html_path.name}.{suffix}.candidate"
            )
            try:
                cancellation.raise_if_cancelled()
                llm_candidate.write_text(llm_payload, encoding="utf-8")
                render_html_report(
                    llm_candidate,
                    project_dir / runtime_artifact.relative_path,
                    html_candidate,
                    on_progress=lambda stage: update_progress(stage, 99),
                )
                cancellation.raise_if_cancelled()
                os.replace(llm_candidate, llm_path)
                os.replace(html_candidate, html_path)
            except DFMError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise DFMError(
                    "report_content_invalid",
                    "The Agent-authored HTML report content could not be materialized.",
                    {"error": str(exc)},
                ) from exc
            finally:
                llm_candidate.unlink(missing_ok=True)
                html_candidate.unlink(missing_ok=True)

            now = _utc_now()
            return [
                ArtifactRecord(
                    f"artifact_{run_id}_report_html_llm",
                    "report_html_llm",
                    llm_path.relative_to(project_dir).as_posix(),
                    "application/x-ndjson",
                    now,
                ),
                ArtifactRecord(
                    f"artifact_{run_id}_report_html",
                    "report_html",
                    html_path.relative_to(project_dir).as_posix(),
                    "text/html; charset=utf-8",
                    now,
                ),
            ]

        queued = self.jobs.start_report(
            project_id,
            run_id,
            render,
            on_update=on_update,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "accepted": queued.status is RunStatus.REPORTING,
            "complete": queued.status is RunStatus.SUCCEEDED,
            "next_action": (
                "result" if queued.status is RunStatus.SUCCEEDED else "status"
            ),
            "run": self._report_run_dict(queued),
        }

    def _tool_progress_listener(
        self,
        project_id: str,
        params: dict[str, Any],
    ) -> Callable[[RunRecord], None]:
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
            html_report = next(
                (
                    item
                    for item in reversed(updated.artifacts)
                    if item.kind == "report_html"
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
                    report_html=(
                        str(
                            (
                                self.workspace.project_dir(project_id)
                                / html_report.relative_path
                            ).resolve()
                        )
                        if html_report is not None
                        else None
                    ),
                    is_error=updated.status in {RunStatus.FAILED, RunStatus.BLOCKED},
                )
            except Exception:
                return

        return on_update

    def project(self, action: str, **params: Any) -> dict[str, Any]:
        if action == "ontology_status":
            return {
                "ok": True,
                "source": "installed_workspace",
                "ontology_database": str(self.ontology_store.path),
                "ontology": self.ontology_store.identity().to_dict(),
            }
        if action == "sync_ontology":
            if self.ontology_synchronizer is None:
                raise DFMError(
                    "ontology_sync_unavailable",
                    "DFM ontology synchronization is not configured.",
                )
            sync = self.ontology_synchronizer.sync_once()
            return {
                "ok": True,
                "source": "remote_publication",
                "sync": sync,
                "ontology_database": str(self.ontology_store.path),
                "ontology": self.ontology_store.identity().to_dict(),
            }
        if action == "create":
            manifest = self.workspace.create_project(
                params.get("name") or "Untitled DFM project",
                params.get("idempotency_key"),
            )
            requested_process = str(params.get("process") or "").strip()
            if requested_process:
                requested_process = self._resolve_process(manifest, requested_process)
                manifest = self._select_process(
                    manifest.project_id, requested_process, "requested"
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
            self._store(project_id).update(
                lambda current: self._resolve_factor_observations(
                    self.discovery.refresh_candidates(current)
                )
            )
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
                "discovery_capability": self.discovery.capability(project_id),
                "preview": preview,
                **(
                    {"viewer_manifest": preview["viewer_manifest"]}
                    if preview.get("status") == "ready"
                    else {}
                ),
            }
        if action == "confirm_fact":
            name, check_id, referenced_feature_id = self._fact_scope_reference(
                str(params.get("fact_name") or "")
            )
            explicit_feature_id = str(params.get("feature_id") or "").strip() or None
            if (
                explicit_feature_id
                and referenced_feature_id
                and explicit_feature_id != referenced_feature_id
            ):
                raise DFMError(
                    "fact_invalid",
                    "feature_id conflicts with the scoped fact reference.",
                )
            feature_id = explicit_feature_id or referenced_feature_id
            if not name:
                raise DFMError("fact_invalid", "fact_name is required.")

            raw_value = params.get("fact_value")
            normalized_value = self._normalize_fact_value(name, raw_value)
            if name == "process":
                if check_id or feature_id:
                    raise DFMError(
                        "fact_invalid",
                        "process cannot be Check- or Feature-scoped.",
                    )
                self.process_registry.get(normalized_value)
            else:
                manifest = self._store(project_id).load()
                process = str(manifest.process or self.config.default_process)
                snapshot = next(
                    (
                        item
                        for item in reversed(manifest.discovery_snapshots)
                        if item.status == "frozen"
                    ),
                    None,
                )
                if check_id and (
                    name not in self._CHECK_SCOPED_FACTS.get(process, ())
                    or snapshot is None
                    or check_id
                    not in self._applicable_analysis_check_ids(manifest, snapshot)
                ):
                    raise DFMError(
                        "fact_invalid",
                        "This factor is not required by the discovered Check.",
                        {"fact_name": name, "check_id": check_id},
                    )
                if feature_id:
                    requirement = next(
                        (
                            item
                            for item in self.process_registry.get(
                                process
                            ).fact_requirements()
                            if self._canonical_fact_name(item.name) == name
                        ),
                        None,
                    )
                    feature_ref = f"{name}@{check_id}" if check_id else name
                    allowed_feature_ids = self.clarification_feature_refs(
                        project_id, feature_ref
                    )
                    if (
                        requirement is None
                        or requirement.impact_scope != "特征实例级"
                        or feature_id not in allowed_feature_ids
                    ):
                        raise DFMError(
                            "fact_invalid",
                            "This factor is not required by the discovered Feature instance.",
                            {"fact_name": name, "feature_id": feature_id},
                        )
                choices = (
                    ()
                    if name == "pull_dir"
                    else self.clarification_choices(
                        process, f"{name}@{check_id}" if check_id else name
                    )
                )
                if check_id and not choices:
                    raise DFMError(
                        "fact_invalid",
                        "The discovered Check has no published choices for this factor.",
                        {"fact_name": name, "check_id": check_id},
                    )
                if choices:
                    if isinstance(normalized_value, str):
                        normalized_value = normalized_value.strip()
                    if normalized_value not in choices:
                        raise DFMError(
                            "fact_invalid",
                            "Fact value is not a published option for this factor.",
                            {
                                "fact_name": name,
                                "value": raw_value,
                                "allowed_values": list(choices),
                            },
                        )

            fact = FactRecord(
                f"fact_{uuid4().hex[:16]}",
                name,
                normalized_value,
                "user",
                "confirmed",
                check_id=check_id,
                feature_id=feature_id,
            )

            def confirm(current: ProjectManifest) -> ProjectManifest:
                plans = []
                for plan in current.plans:
                    affected = self._affected_operations_for_fact(plan, name)
                    geometric_affected = bool(plan.plan_fragments) and (
                        name in {"process", "model_units"}
                        or any(
                            name in binding.required_fact_names
                            for binding in plan.rule_bindings
                            if isinstance(binding, GeometricRuleBinding)
                        )
                    )
                    plans.append(
                        replace(
                            plan,
                            status="invalidated",
                            invalidated_by=f"fact:{name}",
                            affected_operation_ids=affected,
                        )
                        if affected or geometric_affected
                        else plan
                    )
                return replace(
                    current,
                    facts=[*current.facts, fact],
                    observations=[
                        replace(item, status="confirmed_by_user")
                        if check_id is None
                        and feature_id is None
                        and self._observation_factor_name(item) == name
                        and item.value == normalized_value
                        and item.status
                        in {"candidate", "needs_confirmation", "conflict"}
                        else item
                        for item in current.observations
                    ],
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
                        if item.clarification_id
                        == self._clarification_id(
                            name,
                            check_id=check_id,
                            feature_id=feature_id,
                        )
                        else item
                        for item in current.clarifications
                    ],
                    plans=plans,
                    updated_at=_utc_now(),
                )

            manifest = self._store(project_id).update(confirm)
            pending_startup = self._open_clarifications(manifest, phase="discovery")
            if pending_startup:
                next_action = "clarify"
            elif name in self._DISCOVERY_FACT_NAMES or not any(
                item.status == "frozen" for item in manifest.discovery_snapshots
            ):
                next_action = "discover"
            elif self._open_clarifications(manifest, phase="analysis"):
                next_action = "clarify"
            else:
                next_action = "plan"
            response = {
                "ok": True,
                "project_id": project_id,
                "fact": fact.to_dict(),
                "revision": manifest.revision,
                "next_action": next_action,
            }
            return self._after_workflow_update(project_id, response) if name not in self._DISCOVERY_FACT_NAMES else response
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
                "ontology": self.ontology_store.identity().to_dict(),
                "capabilities": self._capabilities(manifest),
                "process_capabilities": process_capabilities,
            }
        raise DFMError("action_invalid", f"Unsupported dfm_project action: {action}")

    def _after_workflow_update(self, project_id: str, response: dict[str, Any]) -> dict[str, Any]:
        """Recheck an explicitly started Run after human input, without LLM routing."""
        if response.get("unchanged"):
            return response
        continued = []
        with self._workflow_lock:
            manifest = self._store(project_id).load()
            for run in manifest.runs:
                if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING} or not run.plan_snapshot:
                    continue
                if run.runtime_id != self.jobs.runtime_id:
                    continue
                if not (run.plan_snapshot.get("plan_fragments") or run.plan_snapshot.get("pending_inputs")):
                    continue
                for attempt in range(3):
                    try:
                        current = self.jobs.status(project_id, run.run_id)
                        if current.status not in {RunStatus.QUEUED, RunStatus.RUNNING} or current.stage == "sealing":
                            break
                        planned = self.analysis("plan", project_id=project_id, base_plan_id=current.plan_id,
                                                analyzer_key=current.analyzer_key)
                        if "plan" not in planned:
                            continued.append({"run_id": run.run_id, "status": planned.get("status", "waiting_input")})
                            break
                        if planned.get("unchanged"):
                            break
                        plan = PlanRecord.from_dict(planned["plan"])
                        if plan.status == "blocked":
                            continued.append({"run_id": run.run_id, "status": "blocked", "capability": planned["capability"]})
                            break
                        updated = self.jobs.append_plan(project_id, run.run_id, plan)
                        continued.append({"run_id": updated.run_id, "plan_id": updated.plan_id,
                                          "status": updated.status.value, "stage": updated.stage})
                        break
                    except DFMError as exc:
                        if exc.code == "manifest_conflict" and attempt < 2:
                            continue
                        continued.append({"run_id": run.run_id, "status": "needs_attention", "reason_code": exc.code})
                        break
        return {**response, **({"workflow": continued} if continued else {}),
                "revision": self._store(project_id).load().revision}

    def analysis(self, action: str, **params: Any) -> dict[str, Any]:
        if self.ontology_synchronizer is not None and action not in {
            "status",
            "cancel",
            "result",
            "review_context",
        }:
            self.ontology_synchronizer.ensure_current_not_revoked()
        project_id = params.get("project_id") or ""
        if action == "review_context":
            return self._review_context(project_id)
        if action == "revise_annotations":
            return self._revise_annotations(project_id, params.get("annotations", []),
                params.get("retire_annotation_ids", []), params.get("expected_revision"), params.get("expected_snapshot_id"))
        if action == "update_forming_scope_geometry":
            return self._after_workflow_update(project_id, self._update_forming_scope_geometry(
                project_id, params.get("forming_scope_id"), params.get("geometry_refs"), params.get("expected_revision"), params.get("expected_snapshot_id")))
        if action == "restructure_forming_scopes":
            return self._after_workflow_update(project_id, self._revise_scope_candidates(
                project_id, params.get("forming_scopes", []), params.get("retire_scope_ids", []), params.get("expected_revision"), params.get("expected_snapshot_id")))
        if action == "analyze_forming_scope":
            return self._analyze_forming_scope(
                project_id,
                params.get("forming_scope_id"),
                params.get("scope_mode"),
                params.get("draw_direction"),
            )
        if action == "revise_forming_scopes":
            return self._after_workflow_update(project_id, self._revise_forming_scopes(
                project_id, params.get("forming_scopes"), params.get("expected_revision"), params.get("expected_snapshot_id")))
        if action == "stage_forming_direction":
            return self._after_workflow_update(project_id, self._stage_forming_direction(project_id,
                params.get("forming_scope"), params.get("expected_revision"), params.get("expected_snapshot_id")))
        if action == "prepare_review":
            return self._prepare_review(project_id, params.get("expected_revision"), params.get("expected_snapshot_id"))
        if action == "update_feature_geometry":
            return self._after_workflow_update(project_id, self._update_discovery_feature_geometry(
                project_id,
                params.get("feature_id"),
                params.get("geometry_refs"),
                params.get("expected_revision"),
                params.get("expected_snapshot_id"),
            ))
        if action == "confirm_discovery":
            raw_ids = params.get("review_group_ids")
            selected = set(raw_ids) if isinstance(raw_ids, list) and all(isinstance(value, str) for value in raw_ids) else set()
            groups = {row.review_group_id: row for row in self._store(project_id).load().review_groups}
            attributes_only = bool(selected) and all(identity in groups and groups[identity].kind == "surface_attribute" for identity in selected)
            result = self._confirm_discovery(
                project_id,
                params.get("expected_revision"),
                params.get("review_group_ids"),
                params.get("expected_snapshot_id"),
            )
            return result if attributes_only else self._after_workflow_update(project_id, result)
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
                if item.status == "confirmed" and item.check_id is None
            }
            confirmed_facts.update({
                item.name: {
                    "value": item.value,
                    "source_ref": f"fact:{item.fact_id}",
                }
                for item in manifest.facts
                if item.status == "confirmed" and item.check_id == check_id
            })
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
            resolved = self._resolve_factor_observations(manifest)
            if resolved is not manifest:
                manifest = store.update(lambda _current: resolved)
            requested_process = self._resolve_process(manifest, params.get("process"))
            if params.get("process") and manifest.process != requested_process:
                manifest = self._select_process(
                    project_id, requested_process, "requested"
                )
            manifest = self._refresh_drawing_ocr(project_id)
            manifest = self._ensure_clarifications(project_id, phase="discovery")
            pending_interpretations = self._pending_drawing_interpretations(manifest)
            if pending_interpretations:
                return {
                    "ok": True,
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
                    "ok": True,
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
                    "ok": True,
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
                        "discovery.worker_features",
                        "discover_geometry_features",
                        depends_on=["discovery.agent_interpretation"],
                        required_fact_names=["process", "model_units", "pull_dir"],
                        feature_refs=list(snapshot.feature_refs),
                    ),
                    PlanOperation(
                        "discovery.fusion",
                        "validate_agent_fusion_links",
                        depends_on=["discovery.worker_features"],
                        feature_refs=list(snapshot.feature_refs),
                    ),
                ],
                phase="discovery",
                discovery_snapshot_refs=[snapshot.snapshot_id],
                regions=[],
            )
            plans = (
                discovered.plans
                if existing_plan is not None
                else [*discovered.plans, discovery_plan]
            )
            review_groups = self._structure_review_groups(discovered, snapshot)
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
                    review_groups=review_groups,
                    plans=plans,
                    updated_at=_utc_now(),
                )
            )
            review = manifest.capabilities.get("discovery_review", {})
            review_confirmed = (
                review.get("status") == "confirmed"
                and review.get("snapshot_id") == snapshot.snapshot_id
            )
            viewer_path = materialize_discovery_viewer_manifest(
                self.workspace.project_dir(project_id),
                manifest,
                review_status="confirmed" if review_confirmed else "pending",
            )
            editable_features = any(
                item.kind != "ordinary_part"
                and item.feature_id in snapshot.feature_refs
                for item in manifest.features
            )
            if not review_confirmed and viewer_path is not None and editable_features:
                review_payload = {
                    "status": "pending",
                    "snapshot_id": snapshot.snapshot_id,
                    "viewer_manifest": str(viewer_path) if viewer_path else None,
                    "edited_feature_ids": list(review.get("edited_feature_ids", [])),
                }
                if review != review_payload:
                    manifest = store.update(
                        lambda current: replace(
                            current,
                            capabilities={
                                **current.capabilities,
                                "discovery_review": review_payload,
                            },
                            updated_at=_utc_now(),
                        ),
                        expected_revision=manifest.revision,
                    )
                viewer_path = materialize_discovery_viewer_manifest(
                    self.workspace.project_dir(project_id),
                    manifest,
                    review_status="pending",
                )
                return self._discovery_response(
                    manifest,
                    snapshot,
                    discovery_plan,
                    status="discovery_review_required",
                    requires_user_response=True,
                    next_action="confirm_discovery",
                    revision=manifest.revision,
                    viewer_manifest=str(viewer_path) if viewer_path else None,
                    open_clarifications=[],
                )
            manifest = self._ensure_clarifications(project_id, phase="analysis")
            return self._discovery_response(manifest, snapshot, discovery_plan)
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
            requested_process = self._resolve_process(manifest, params.get("process"))
            incremental_geometry = str(analyzer_key) == "occt_cpp" and requested_process == "injection"
            if manifest.process != requested_process:
                manifest = self._select_process(
                    project_id,
                    requested_process,
                    "requested" if params.get("process") else manifest.process_source,
                )
            manifest = self._ensure_clarifications(project_id, phase="discovery")
            discovery_clarifications = self._open_clarifications(
                manifest, requested_process, phase="discovery"
            )
            if discovery_clarifications:
                return {
                    "ok": True,
                    "project_id": project_id,
                    "status": "clarification_required",
                    "phase": "discovery",
                    "requires_user_response": True,
                    "next_action": "clarify",
                    "do_not_infer": True,
                    "clarifications": [
                        item.to_dict() for item in discovery_clarifications
                    ],
                }
            review = manifest.capabilities.get("discovery_review", {})
            if review.get("status") == "pending" and not incremental_geometry:
                return {
                    "ok": True,
                    "project_id": project_id,
                    "status": "discovery_review_required",
                    "phase": "discovery",
                    "requires_user_response": True,
                    "next_action": "confirm_discovery",
                    "revision": manifest.revision,
                    "viewer_manifest": review.get("viewer_manifest"),
                }
            manifest = self._ensure_clarifications(project_id, phase="analysis")
            discovery_snapshot = self._latest_discovery_snapshot(manifest)
            if discovery_snapshot is None:
                return {
                    "ok": True,
                    "project_id": project_id,
                    "status": "discovery_required",
                    "phase": "discovery",
                    "requires_user_response": False,
                    "next_action": "discover",
                }
            if (
                review.get("status") == "confirmed"
                and review.get("snapshot_id") != discovery_snapshot.snapshot_id
                and not incremental_geometry
            ):
                return {
                    "ok": True,
                    "project_id": project_id,
                    "status": "discovery_review_required",
                    "phase": "discovery",
                    "requires_user_response": True,
                    "next_action": "confirm_discovery",
                    "revision": manifest.revision,
                    "viewer_manifest": review.get("viewer_manifest"),
                }
            open_clarifications = self._open_clarifications(
                manifest, requested_process, phase="analysis"
            )
            if open_clarifications and not incremental_geometry:
                return {
                    "ok": True,
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
            if process == "injection" and str(analyzer_key) != "occt_cpp":
                raise DFMError(
                    "geometry_contract_unsupported",
                    "The selected injection backend does not implement the required "
                    "persistent dfm.geometry/v2 Feature/G/context contract.",
                    {
                        "analyzer_key": str(analyzer_key),
                        "required_contract": "dfm.geometry/v2",
                    },
                )
            if str(analyzer_key) == "occt_cpp":
                self._ensure_occt_geometry_capability(project_id)
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
                    and fact.check_id is None
                    and fact.feature_id is None
                    and fact.name in defaults.accepted_inputs
                }
                scoped_facts: dict[str, dict[str, Any]] = {}
                for fact in manifest.facts:
                    if (
                        fact.status == "confirmed"
                        and fact.check_id is not None
                        and fact.feature_id is None
                    ):
                        scoped_facts.setdefault(fact.check_id, {})[fact.name] = (
                            fact.value
                        )

                def compile_with_scope(check_facts):
                    scoped_kwargs = {"scoped_facts": check_facts} if check_facts else {}
                    compiled = (
                        compile_occt_injection_plan(
                            adapter,
                            context,
                            raw_parameters,
                            self.occt_geometry_capability,
                            **scoped_kwargs,
                        )
                        if str(analyzer_key) == "occt_cpp" and process == "injection"
                        else adapter.compile(context, raw_parameters, **scoped_kwargs)
                        if process == "injection"
                        else adapter.compile(context, raw_parameters)
                    )
                    return self._bind_geometric_scope(
                        compiled,
                        manifest,
                        discovery_snapshot,
                    )

                process_plan = compile_with_scope(scoped_facts)
                feature_facts = [
                    fact
                    for fact in manifest.facts
                    if fact.status == "confirmed" and fact.feature_id is not None
                ]
                if feature_facts:
                    checks_by_feature: dict[str, set[str]] = {}
                    for target in self._geometric_analysis_scopes(
                        manifest, discovery_snapshot
                    ):
                        feature = target.get("feature")
                        feature_id = str(getattr(feature, "feature_id", "") or "")
                        if not feature_id:
                            continue
                        checks = checks_by_feature.setdefault(feature_id, set())
                        checks.add(str(target["check_id"]))

                    plans_by_feature = {}
                    for feature_id in dict.fromkeys(
                        str(fact.feature_id) for fact in feature_facts
                    ):
                        feature_scope = {
                            check_id: dict(values)
                            for check_id, values in scoped_facts.items()
                        }
                        for fact in feature_facts:
                            if fact.feature_id != feature_id:
                                continue
                            check_ids = (
                                {fact.check_id}
                                if fact.check_id
                                else checks_by_feature.get(feature_id, set())
                            )
                            for check_id in check_ids:
                                feature_scope.setdefault(check_id, {})[fact.name] = (
                                    fact.value
                                )
                        plans_by_feature[feature_id] = compile_with_scope(feature_scope)

                    bindings = [
                        binding
                        for binding in process_plan.rule_bindings
                        if not set(binding.feature_refs).intersection(plans_by_feature)
                    ]
                    for feature_id, feature_plan in plans_by_feature.items():
                        bindings.extend(
                            binding
                            for binding in feature_plan.rule_bindings
                            if feature_id in binding.feature_refs
                        )

                    referenced_operation_ids = {
                        operand.operation_id
                        for binding in bindings
                        for operand in binding.measurement_operands()
                        if not isinstance(operand, GeometricRuleOperand)
                    }
                    all_plans = [process_plan, *plans_by_feature.values()]
                    operations_by_id = {
                        operation.operation_id: operation
                        for plan in all_plans
                        for operation in plan.operations
                        if not operation.metric_ids
                        or operation.operation_id in referenced_operation_ids
                    }
                    rule_ids = {binding.rule_id for binding in bindings}
                    rules = {
                        rule_id: rule
                        for plan in all_plans
                        for rule_id, rule in plan.rules.items()
                        if rule_id in rule_ids
                    }
                    process_plan = replace(
                        process_plan,
                        rule_bindings=bindings,
                        operations=(
                            []
                            if any(
                                isinstance(operand, GeometricRuleOperand)
                                for binding in bindings
                                for operand in binding.measurement_operands()
                            )
                            else list(operations_by_id.values())
                        ),
                        rules=rules,
                    )
                if (
                    str(analyzer_key) == "occt_cpp"
                    and process == "injection"
                    and not process_plan.rule_bindings
                    and not open_clarifications
                ):
                    supported_geometric_ids = sorted(
                        geometry_binding_index(self.occt_geometry_capability or {})
                    )
                    return {
                        "ok": True,
                        "project_id": project_id,
                        "status": "no_applicable_checks",
                        "phase": "analysis",
                        "requires_user_response": False,
                        "next_action": "complete",
                        "reason_code": "no_applicable_supported_rules",
                        "message": (
                            "No released rule could be bound for the confirmed facts, "
                            "discovered features, and available geometry capabilities. "
                            "This result alone does not establish that an OCCT algorithm "
                            "is missing."
                        ),
                        "ontology": self.ontology_store.identity().to_dict(),
                        "published_check_ids": list(
                            self.ontology_store.check_ids(process)
                        ),
                        "discovery_snapshot_ref": discovery_snapshot.snapshot_id,
                        "supported_geometric_ids": supported_geometric_ids,
                        "capability": capability.to_dict(),
                    }
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
            if parent_plan is not None and parent_plan.status not in {"invalidated", "ready", "waiting_input"}:
                raise DFMError(
                    "plan_not_rebuildable",
                    "Only an active or invalidated plan can be continued incrementally.",
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
            geometric_plan = incremental_geometry or bool(
                process_plan
                and any(
                    isinstance(operand, GeometricRuleOperand)
                    for binding in process_plan.rule_bindings
                    for operand in binding.measurement_operands()
                )
            )
            rule_bindings = (
                list(process_plan.rule_bindings)
                if geometric_plan
                else [
                    item
                    for item in process_plan.rule_bindings
                    if {
                        operand.operation_id
                        for operand in item.measurement_operands()
                        if not isinstance(operand, GeometricRuleOperand)
                    }.issubset(operation_ids)
                ]
                if process_plan
                else []
            )
            active_inputs = self._active_inputs(manifest)
            now = _utc_now()
            plan_fragments: list[PlanFragmentRecord] = []
            if geometric_plan:
                capability_index = geometry_binding_index(
                    self.occt_geometry_capability or {}
                )
                requests_by_group: dict[
                    tuple[str, str, str, str, str], dict[tuple[str, str], set[str]]
                ] = {}
                contexts_by_group: dict[
                    tuple[str, str, str, str, str], dict[tuple[str, str], dict[str, Any]]
                ] = {}
                binding_refs_by_group: dict[tuple[str, str, str, str, str], set[str]] = {}
                review_refs_by_group: dict[tuple[str, str, str, str, str], set[str]] = {}
                reference_dependencies_by_group: dict[tuple[str, str, str, str, str], list[dict]] = {}
                blocking_by_group: dict[
                    tuple[str, str, str, str, str], list[dict[str, Any]]
                ] = {}
                coverage_gaps: list[dict[str, Any]] = []
                features_by_id = {item.feature_id: item for item in manifest.features}
                active_groups = [
                    item
                    for item in self._structure_review_groups(manifest, discovery_snapshot)
                    if item.status in {"pending_review", "confirmed"}
                ]
                relationship_index = {}
                for relationship in discovery_snapshot.relationships:
                    key = (relationship.get("from_ref"), relationship.get("type"))
                    relationship_index.setdefault(key, []).append(relationship)
                ordinary_review_index = {}
                for group in active_groups:
                    if group.kind == "ordinary_structure":
                        for target in group.target_refs:
                            ordinary_review_index.setdefault(target, []).append(group)
                for binding in rule_bindings:
                    for operand in binding.measurement_operands():
                        if len(operand.feature_refs) != 1:
                            raise DFMError(
                                "plan_fragment_invalid",
                                "Each Geometric operand must resolve to one Worker feature.",
                                {
                                    "binding_id": binding.binding_id,
                                    "operand_alias": operand.alias,
                                },
                            )
                        declaration = capability_index.get(operand.geometric_id, {})
                        required_context = set(
                            declaration.get("context_schema", {}).get("required", [])
                        )
                        feature = features_by_id[operand.feature_refs[0]]
                        scope_id = str(operand.context.get("forming_scope_id") or "")
                        scope_key = scope_id or "ordinary"
                        required_groups = [item for item in active_groups
                                           if item.kind == "ordinary_structure" and feature.feature_id in item.target_refs]
                        required_kinds = ({"undercut_scope", "local_structure", "local_direction"}
                                          if scope_id and any(raw.get("forming_scope_id") == scope_id and raw.get("mode") == "local"
                                                              for raw in discovery_snapshot.forming_scopes)
                                          else {"product_forming"} if scope_id else set())
                        required_groups.extend(item for item in active_groups
                                               if item.kind in required_kinds and scope_id in item.target_refs)
                        review_refs = {item.review_group_id for item in required_groups}
                        blocking_reasons: list[dict[str, Any]] = []
                        if required_context - set(operand.context):
                            blocking_reasons.append({
                                "code": "missing_context",
                                "geometric_id": operand.geometric_id,
                                "missing_fields": sorted(
                                    required_context - set(operand.context)
                                ),
                            })
                        missing_kinds = required_kinds - {item.kind for item in required_groups}
                        if (not any(item.kind == "ordinary_structure" for item in required_groups)
                            or any(item.status != "confirmed" and item.kind != "local_direction" for item in required_groups)
                            or missing_kinds - {"local_direction"}):
                            blocking_reasons.append({
                                "code": "pending_review",
                                "feature_id": feature.feature_id,
                                "forming_scope_id": scope_id or None,
                                "review_group_refs": sorted(item.review_group_id for item in required_groups if item.status != "confirmed"),
                                "missing_group_kinds": sorted(missing_kinds),
                            })
                        reference_dependencies, reference_reviews, reference_blockers = resolve_reference_dependencies(
                            feature, operand.geometric_id, declaration["requires_relationships"],
                            features=features_by_id, relationships=relationship_index, reviews=ordinary_review_index,
                        )
                        review_refs.update(group.review_group_id for group in reference_reviews)
                        blocking_reasons.extend(reference_blockers)
                        for inventory_kind in declaration.get("requires_complete_inventory", []):
                            inventory_context = (
                                {"forming_scope_id": scope_id} if scope_id else {}
                            )
                            coverage = next(
                                (
                                    item
                                    for item in discovery_snapshot.coverage
                                    if item.get("feature_kind") == inventory_kind
                                    and item.get("context", {}) == inventory_context
                                ),
                                None,
                            )
                            if coverage is None or coverage.get("status") != "complete":
                                gap = {
                                    "feature_kind": inventory_kind,
                                    "context": inventory_context,
                                    "geometric_id": operand.geometric_id,
                                    "status": (
                                        coverage.get("status")
                                        if isinstance(coverage, dict)
                                        else "missing"
                                    ),
                                    "unresolved_geometry_ref": (
                                        coverage.get("unresolved_geometry_ref")
                                        if isinstance(coverage, dict)
                                        else None
                                    ),
                                }
                                if gap not in coverage_gaps:
                                    coverage_gaps.append(gap)
                                blocking_reasons.append({
                                    "code": "incomplete_inventory",
                                    **gap,
                                })
                        fragment_status = (
                            "pending_review"
                            if any(
                                item["code"] == "pending_review"
                                for item in blocking_reasons
                            )
                            else "needs_attention"
                            if any(item["code"] in {"missing_reference_relationship", "ambiguous_reference_relationship", "invalid_reference_relationship"}
                                   for item in blocking_reasons)
                            else "waiting_input"
                            if blocking_reasons
                            else "ready"
                        )
                        context_json = json.dumps(
                            operand.context,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        group_key = (fragment_status, scope_key, feature.feature_id, operand.geometric_id, context_json)
                        for dependency in reference_dependencies:
                            if dependency not in reference_dependencies_by_group.setdefault(group_key, []):
                                reference_dependencies_by_group[group_key].append(dependency)
                        key = (operand.feature_refs[0], context_json)
                        requests_by_group.setdefault(group_key, {}).setdefault(
                            key, set()
                        ).add(operand.geometric_id)
                        contexts_by_group.setdefault(group_key, {})[key] = dict(
                            operand.context
                        )
                        binding_refs_by_group.setdefault(group_key, set()).add(
                            binding.binding_id
                        )
                        review_refs_by_group.setdefault(group_key, set()).update(
                            review_refs
                        )
                        for reason in blocking_reasons:
                            if reason not in blocking_by_group.setdefault(group_key, []):
                                blocking_by_group[group_key].append(reason)
                capability_version = geometry_capability_identity(
                    self.occt_geometry_capability or {}
                )
                for group_key in sorted(requests_by_group):
                    fragment_status = group_key[0]
                    requests = requests_by_group[group_key]
                    contexts = contexts_by_group[group_key]
                    items = [
                        GeometricRequest(
                            feature_id=feature_id,
                            geometric_ids=sorted(geometric_ids),
                            context=contexts[(feature_id, context_json)],
                        )
                        for (feature_id, context_json), geometric_ids in sorted(
                            requests.items()
                        )
                    ]
                    grouped_binding_refs = binding_refs_by_group[group_key]
                    grouped_bindings = [
                        binding
                        for binding in rule_bindings
                        if binding.binding_id in grouped_binding_refs
                    ]
                    grouped_feature_ids = {
                        item.feature_id for item in items
                    }
                    required_fact_names = {
                        "process",
                        "model_units",
                        *(
                            name
                            for binding in grouped_bindings
                            for name in binding.required_fact_names
                        ),
                    }
                    grouped_check_ids = {
                        binding.check_id for binding in grouped_bindings
                    }
                    confirmed_fact_refs = sorted(
                        fact.fact_id
                        for fact in manifest.facts
                        if fact.status == "confirmed"
                        and fact.name in required_fact_names
                        and (fact.check_id is None or fact.check_id in grouped_check_ids)
                        and (
                            fact.feature_id is None
                            or fact.feature_id in grouped_feature_ids
                        )
                    )
                    fragment_identity = {
                        "status": fragment_status,
                        "snapshot_id": discovery_snapshot.worker_snapshot_id,
                        "source_discovery_snapshot_id": (
                            discovery_snapshot.snapshot_id
                        ),
                        "capability_version": capability_version,
                        "confirmed_fact_refs": confirmed_fact_refs,
                        "rule_binding_refs": sorted(grouped_binding_refs),
                        "review_group_refs": sorted(
                            review_refs_by_group.get(group_key, set())
                        ),
                        "blocking_reasons": blocking_by_group.get(group_key, []),
                        "reference_dependencies": sorted(reference_dependencies_by_group.get(group_key, []),
                                                         key=lambda row: json.dumps(row, sort_keys=True)),
                        "items": [item.to_dict() for item in items],
                        "ontology_snapshot_sha256": (
                            process_plan.ontology_snapshot_sha256
                        ),
                    }
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            fragment_identity,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    plan_fragments.append(
                        PlanFragmentRecord(
                            fragment_id=f"fragment_{fingerprint[:16]}",
                            snapshot_id=fragment_identity["snapshot_id"],
                            source_discovery_snapshot_id=fragment_identity[
                                "source_discovery_snapshot_id"
                            ],
                            capability_version=fragment_identity["capability_version"],
                            dependency_fingerprint=fingerprint,
                            confirmed_fact_refs=fragment_identity[
                                "confirmed_fact_refs"
                            ],
                            rule_binding_refs=fragment_identity["rule_binding_refs"],
                            items=items,
                            status=fragment_status,
                            review_group_refs=fragment_identity[
                                "review_group_refs"
                            ],
                            blocking_reasons=fragment_identity[
                                "blocking_reasons"
                            ],
                            reference_dependencies=fragment_identity["reference_dependencies"],
                        )
                    )
                if parent_plan is not None and parent_plan.plan_fragments:
                    active_parent_fragments = parent_plan.active_plan_fragments()
                    parent_bindings = {
                        item.binding_id: item for item in parent_plan.rule_bindings
                    }

                    def fragment_keys(fragment):
                        return {
                            (
                                item.feature_id,
                                geometric_id,
                                item.context.get("forming_scope_id"),
                            )
                            for item in fragment.items
                            for geometric_id in item.geometric_ids
                        }

                    def fragment_was_affected(fragment):
                        reason = str(parent_plan.invalidated_by or "")
                        if reason.startswith("forming_scope:"):
                            scope_ids = set(
                                reason.removeprefix("forming_scope:").split(",")
                            )
                            return any(
                                item.context.get("forming_scope_id") in scope_ids
                                for item in fragment.items
                            )
                        if reason.startswith("feature:"):
                            feature_ids = set(reason.removeprefix("feature:").split(","))
                            return any(
                                item.feature_id in feature_ids
                                for item in fragment.items
                            ) or any(dependency["target_feature_id"] in feature_ids for dependency in fragment.reference_dependencies)
                        if reason.startswith("fact:"):
                            fact_name = reason.removeprefix("fact:")
                            if fact_name in {"process", "model_units"}:
                                return True
                            return any(
                                fact_name in parent_bindings[ref].required_fact_names
                                for ref in fragment.rule_binding_refs
                                if ref in parent_bindings
                            )
                        return True

                    appended_fragments: list[PlanFragmentRecord] = []
                    for fragment in plan_fragments:
                        new_keys = fragment_keys(fragment)
                        superseded = next(
                            (
                                item
                                for item in active_parent_fragments
                                if fragment_keys(item).intersection(new_keys)
                            ),
                            None,
                        )
                        if superseded is not None and (
                            superseded.dependency_fingerprint
                            == fragment.dependency_fingerprint
                            or not fragment_was_affected(superseded)
                        ):
                            continue
                        supersedes = (
                            superseded.fragment_id
                            if superseded is not None
                            else None
                        )
                        fragment = replace(fragment, supersedes=supersedes)
                        if any(
                            item.fragment_id == fragment.fragment_id
                            for item in parent_plan.plan_fragments
                        ):
                            replacement_identity = hashlib.sha256(
                                json.dumps(
                                    {
                                        "dependency_fingerprint": (
                                            fragment.dependency_fingerprint
                                        ),
                                        "supersedes": supersedes,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest()
                            fragment = replace(
                                fragment,
                                fragment_id=f"fragment_{replacement_identity[:16]}",
                            )
                        appended_fragments.append(fragment)
                    retired = {str(raw["forming_scope_id"]) for raw in discovery_snapshot.forming_scopes if raw.get("status") == "stale"}
                    for fragment in active_parent_fragments:
                        if any(item.context.get("forming_scope_id") in retired for item in fragment.items):
                            digest = hashlib.sha256((fragment.fragment_id + ":retired:" + discovery_snapshot.snapshot_id).encode()).hexdigest()
                            appended_fragments.append(replace(fragment, fragment_id="fragment_" + digest[:16],
                                dependency_fingerprint=digest, status="stale", supersedes=fragment.fragment_id,
                                blocking_reasons=[{"code": "forming_scope_retired"}]))
                    plan_fragments = [
                        *parent_plan.plan_fragments,
                        *appended_fragments,
                    ]
                    merged_bindings = {
                        item.binding_id: item for item in parent_plan.rule_bindings
                    }
                    for binding in rule_bindings:
                        previous = merged_bindings.get(binding.binding_id)
                        if previous is not None and previous != binding:
                            raise DFMError(
                                "plan_rule_binding_invalid",
                                "An immutable rule binding identity changed during plan append.",
                                {"binding_id": binding.binding_id},
                            )
                        merged_bindings[binding.binding_id] = binding
                    rule_bindings = list(merged_bindings.values())
                    merged_rules = dict(parent_plan.rules)
                    for rule_id, rule in process_plan.rules.items():
                        previous = merged_rules.get(rule_id)
                        if previous is not None and previous != rule:
                            raise DFMError(
                                "plan_rule_binding_invalid",
                                "An immutable rule identity changed during plan append.",
                                {"rule_id": rule_id},
                            )
                        merged_rules[rule_id] = rule
                    process_plan = replace(process_plan, rules=merged_rules)
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
                operations=[] if geometric_plan else operations,
                parent_plan_id=parent_plan_id,
                phase="analysis",
                discovery_snapshot_refs=[discovery_snapshot.snapshot_id],
                regions=[]
                if geometric_plan
                else [
                    item
                    for item in manifest.regions
                    if item.region_id in discovery_snapshot.region_refs
                ],
                plan_fragments=plan_fragments,
                coverage_gaps=coverage_gaps if geometric_plan else [],
                pending_inputs=[{
                    **item.to_dict(),
                    **dict(zip(("fact_name", "check_id", "feature_id"), self._fact_scope_reference(
                        item.clarification_id.removeprefix("clarification_")
                    ))),
                } for item in open_clarifications] if geometric_plan else [],
            )
            capability = analyzer.capability(self._context(manifest, plan))
            plan = replace(
                plan,
                status=(
                    "blocked"
                    if capability.status.value != "available"
                    else "waiting_input"
                    if geometric_plan
                    and (plan_fragments or open_clarifications)
                    and not any(item.status == "ready" for item in plan.active_plan_fragments())
                    else "ready"
                ),
            )
            if parent_plan is not None and (plan.plan_fragments == parent_plan.plan_fragments
                                           and plan.pending_inputs == parent_plan.pending_inputs):
                return {"ok": True, "project_id": project_id, "unchanged": True,
                        "plan": parent_plan.to_dict(), "capability": capability.to_dict()}
            store.update(
                lambda current: replace(
                    current, plans=[*current.plans, plan], updated_at=_utc_now()
                ), expected_revision=manifest.revision,
            )
            return {
                "ok": True,
                "project_id": project_id,
                "plan": plan.to_dict(),
                "capability": capability.to_dict(),
            }
        if action == "start":
            plan_id = str(params.get("plan_id") or "").strip()
            if not plan_id:
                raise DFMError(
                    "plan_id_required",
                    "Starting DFM analysis requires the explicit plan_id returned by planning.",
                    {"action": "start"},
                )
            manifest = self._store(project_id).load()
            plan = next(
                (item for item in manifest.plans if item.plan_id == plan_id), None
            )
            if plan is None:
                raise DFMError(
                    "plan_not_found",
                    "DFM analysis plan was not found.",
                    {"plan_id": plan_id},
                )
            if plan.phase != "analysis":
                raise DFMError(
                    "plan_not_ready",
                    "The selected plan is a discovery plan; create an analysis plan before starting a run.",
                    {
                        "plan_id": plan.plan_id,
                        "phase": plan.phase,
                        "status": plan.status,
                        "next_action": "plan",
                    },
                )
            if plan.status == "waiting_input":
                raise DFMError(
                    "plan_not_ready",
                    "The selected plan has no runnable Geometric fragment.",
                    {
                        "plan_id": plan.plan_id,
                        "status": plan.status,
                        "next_action": "confirm_forming_scope",
                    },
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
            active_parent_run = next(
                (
                    item
                    for item in manifest.runs
                    if item.plan_id == plan.parent_plan_id
                    and item.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                ),
                None,
            )
            run = (
                self.jobs.append_plan(
                    project_id,
                    active_parent_run.run_id,
                    plan,
                )
                if active_parent_run is not None
                else self.jobs.start(
                    project_id,
                    plan.analyzer_keys[0],
                    plan=plan,
                    idempotency_key=params.get("idempotency_key"),
                    on_update=self._tool_progress_listener(project_id, params),
                )
            )
            return {
                "ok": True,
                "project_id": project_id,
                "run": self._run_dict(project_id, run),
            }
        run_id = self._resolve_run_id(
            self._store(project_id).load(), params.get("run_id"), action
        )
        if action == "report_context":
            return self._report_context(
                project_id,
                run_id,
                params.get("wait_seconds", 0),
            )
        if action == "render_html":
            return self._render_html(
                project_id,
                run_id,
                params.get("llm_content"),
                on_update=self._tool_progress_listener(project_id, params),
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

    @staticmethod
    def _report_run_dict(run: RunRecord) -> dict[str, Any]:
        """The report author needs status, not the plan and every raw artifact."""
        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "stage": run.stage,
            "progress_percent": run.progress_percent,
            "error": run.error,
        }

    @staticmethod
    def _report_issue_ids(runtime: dict[str, Any]) -> list[str]:
        report = runtime.get("report")
        issues = report.get("issues") if isinstance(report, dict) else None
        if not isinstance(issues, list) or any(
            not isinstance(issue, dict)
            or not isinstance(issue.get("id"), str)
            or not issue["id"]
            for issue in issues
        ):
            raise DFMError(
                "report_input_invalid",
                "The HTML report Runtime must contain issues with stable IDs.",
            )
        ids = [issue["id"] for issue in issues]
        if len(ids) != len(set(ids)):
            raise DFMError(
                "report_input_invalid",
                "The HTML report Runtime contains duplicate issue IDs.",
            )
        return ids

    def close(self) -> None:
        if self._ontology_background_sync is not None:
            self._ontology_background_sync.close()
        self.jobs.shutdown()
        self.geometry_sessions.close()
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
