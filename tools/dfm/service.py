"""Application service behind the stable Hermes DFM tool schemas."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.context_references import parse_context_references
from hermes_constants import get_hermes_home

from .analyzers.base import AnalyzerContext, CancellationToken
from .analyzers.registry import AnalyzerRegistry, build_default_registry
from .config import DFMConfig, load_dfm_config
from .contracts import (
    ClarificationRecord,
    FactRecord,
    InputRecord,
    PlanOperation,
    PlanRecord,
    ProjectManifest,
    RunRecord,
    RunStatus,
)
from .discovery import DiscoveryEngine
from .errors import DFMError
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
    if len(references) == 1 and references[0].kind == "file" and references[0].raw == value:
        value = references[0].target
    elif len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'":
        value = value[1:-1]

    path = Path(os.path.expanduser(value))
    if not path.is_absolute() and working_dir:
        path = Path(str(working_dir)).expanduser() / path
    return path.resolve()


class DFMService:
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
    
    def __init__(self, *, config: DFMConfig | None = None, workspace: DFMWorkspace | None = None, registry: AnalyzerRegistry | None = None, process_registry: ProcessAdapterRegistry | None = None, ontology_store: LocalOntologyStore | None = None, reconcile_jobs: bool = True) -> None:
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
        self.discovery = DiscoveryEngine(ontology_store=self.ontology_store)
        self.jobs = JobManager(self.workspace, self.registry, self.config, reconcile=reconcile_jobs)

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
                analyzer_keys=["occt"],
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
            artifacts = self.registry.get("occt").run(context, CancellationToken())
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
        normalized = str(fact_name or "").strip().lower().replace("-", "_").replace(" ", "_")
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
        superseded = {item.supersedes_input_id for item in manifest.inputs if item.supersedes_input_id}
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
        return {key: self.registry.get(key).capability(context).to_dict() for key in self.registry.keys()}

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
        adapter = self.process_registry.get(process or manifest.process or self.config.default_process)
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
                result.append(item or ClarificationRecord(clarification_id, question, "open"))
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
            item.to_dict()
            for item in self._open_clarifications(manifest, phase=phase)
        ]
        payload["discovery_capability"] = self.discovery.capability()
        return payload

    def _latest_discovery_snapshot(self, manifest: ProjectManifest):
        input_hashes = {
            item.input_id: item.sha256 for item in self._active_inputs(manifest)
        }
        discovery_fact_names = {"process", *self.discovery.required_fact_names()}
        latest_discovery_facts = {
            fact.name: fact.fact_id
            for fact in manifest.facts
            if fact.status == "confirmed" and fact.name in discovery_fact_names
        }
        return next(
            (
                item
                for item in reversed(manifest.discovery_snapshots)
                if item.input_hashes == input_hashes
                and item.process == manifest.process
                and item.status == "frozen"
                and set(latest_discovery_facts.values()).issubset(
                    set(item.confirmed_fact_refs)
                )
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

    def _select_process(self, project_id: str, process: str, source: str) -> ProjectManifest:
        self.process_registry.get(process)
        return self._store(project_id).update(
            lambda current: current
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
                        affected_operation_ids=[item.operation_id for item in plan.operations],
                    )
                    for plan in current.plans
                ],
                updated_at=_utc_now(),
            )
        )

    @staticmethod
    def _resolve_run_id(manifest: ProjectManifest, requested: object, action: str) -> str:
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
            manifest = self.workspace.create_project(params.get("name") or "Untitled DFM project", params.get("idempotency_key"))
            requested_process = str(params.get("process") or "").strip()
            if requested_process:
                manifest = self._select_process(
                    manifest.project_id, requested_process, "user_selected"
                )
            return {"ok": True, "project_id": manifest.project_id, "project": manifest.to_dict()}
        if action == "list":
            projects = []
            if self.workspace.projects_dir.exists():
                for path in sorted(self.workspace.projects_dir.glob("dfm_*/project_manifest.json")):
                    try:
                        projects.append(ManifestStore(path.parent).load().to_dict())
                    except DFMError:
                        continue
            return {"ok": True, "projects": projects}

        project_id = params.get("project_id") or ""
        if action == "add_input":
            source_path = _resolve_input_path(params.get("path"), params.get("working_dir"))
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
                    for item in self._open_clarifications(
                        manifest, phase="discovery"
                    )
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
            
            fact = FactRecord(f"fact_{uuid4().hex[:16]}", name, normalized_value, "user", "confirmed")
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
                        "user_confirmed" if name == "process" else current.process_source
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
            return {"ok": True, "project_id": project_id, "fact": fact.to_dict(), "revision": manifest.revision}
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
            manifest = self._ensure_clarifications(
                project_id, phase="discovery"
            )
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
                    "clarifications": [
                        item.to_dict() for item in open_clarifications
                    ],
                }
            discovered, snapshot = self.discovery.freeze(manifest)
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
                        "discovery.drawing_observations",
                        "extract_drawing_observations_placeholder",
                    ),
                    PlanOperation(
                        "discovery.generic_geometry",
                        "recognize_ordinary_region",
                        depends_on=["discovery.drawing_observations"],
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
                        "fuse_observations_placeholder",
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
                "capability": self.discovery.capability(),
                "open_clarifications": [
                    item.to_dict()
                    for item in self._open_clarifications(
                        manifest, phase="analysis"
                    )
                ],
            }
        if action == "plan":
            store = self._store(project_id)
            manifest = self._reconcile_clarifications(project_id)
            analyzer_key = params.get("analyzer_key") or manifest.input_mode
            if not analyzer_key:
                raise DFMError("input_required", "Register a DFM input before planning analysis.")
            requested_process = str(params.get("process") or manifest.process or self.config.default_process)
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
            if str(analyzer_key) in {"step", "parasolid", "occt"}:
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
                    if str(analyzer_key) == "occt" and process == "injection"
                    else adapter.compile(context, raw_parameters)
                )
                process_plan = self._bind_discovery_scope(
                    process_plan,
                    manifest,
                    discovery_snapshot,
                    preserve_single_operation_ids=str(analyzer_key) == "occt",
                )
            parent_plan_id = str(params.get("base_plan_id") or "") or None
            parent_plan = next(
                (item for item in manifest.plans if item.plan_id == parent_plan_id), None
            )
            if parent_plan_id and parent_plan is None:
                raise DFMError("plan_not_found", "Base DFM analysis plan was not found.", {"plan_id": parent_plan_id})
            if parent_plan is not None and parent_plan.status != "invalidated":
                raise DFMError("plan_not_rebuildable", "Only an invalidated plan can be rebuilt incrementally.", {"plan_id": parent_plan_id, "status": parent_plan.status})
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
                        operand.operation_id
                        for operand in item.measurement_operands()
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
                process_adapter_version=process_plan.adapter_version if process_plan else "",
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
            store.update(lambda current: replace(current, plans=[*current.plans, plan], updated_at=_utc_now()))
            return {"ok": True, "project_id": project_id, "plan": plan.to_dict(), "capability": capability.to_dict()}
        if action == "start":
            manifest = self._store(project_id).load()
            plan_id = params.get("plan_id")
            plan = next((item for item in manifest.plans if item.plan_id == plan_id), None)
            if plan is None:
                raise DFMError("plan_not_found", "DFM analysis plan was not found.", {"plan_id": plan_id})
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
                        "background.tool.complete" if terminal else "background.tool.progress",
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
            return {"ok": True, "project_id": project_id, "run": self._run_dict(project_id, run)}
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
            raise DFMError("action_invalid", f"Unsupported dfm_analysis action: {action}")
        return {"ok": True, "project_id": project_id, "run": self._run_dict(project_id, run)}

    def _run_dict(self, project_id: str, run: RunRecord) -> dict[str, Any]:
        payload = run.to_dict()
        project_dir = self.workspace.project_dir(project_id)
        payload["artifacts"] = [{**artifact.to_dict(), "path": str((project_dir / artifact.relative_path).resolve())} for artifact in run.artifacts]
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
