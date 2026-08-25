---
name: dfm-analysis
description: Use when analyzing injection-molded part manufacturability from STEP/STP CAD or inspecting PDF/PNG/JPG engineering drawings with the built-in dfm_project and dfm_analysis tools.
license: MIT
metadata:
  hermes:
    tags: [DFM, manufacturing, STEP, OCCT, CAD, engineering-drawing]
    requires_toolsets: [dfm]
---

# DFM Analysis

Manage every analysis as a durable DFM project. Tools are the source of
engineering facts; conversation is used to obtain missing facts and explain
traceable results.

## Workflow

The control boundary is `Agent -> discover -> Agent -> plan -> Agent -> start`.
Discovery and planning are deterministic service operations. The Agent must
inspect the frozen DiscoverySnapshot, persisted analysis plan, and capability
before deciding whether execution is valid. Starting a plan does not bypass
those decisions or return control to the Agent Loop from inside the worker.

1. Call `dfm_project` with `create`, unless continuing a known `project_id`.
2. Call `dfm_project` with `add_input` for every STEP/STP or drawing `@file:` reference. Parasolid x_t and NX inputs are deferred and are rejected explicitly; do not rename, convert, or silently route them through the STEP path.
3. Call project `status`. Inspect the input mode and every analyzer `capability`.
4. Pass `process=injection` or `process=die_casting` when the user has selected it; never infer the process from part shape. Call `dfm_analysis` with `discover`. If it returns `status=clarification_required`, it is a hard stop: do **not** answer the questions yourself and do **not** call `confirm_fact` in the same turn. Call the Hermes `clarify` tool for each open question so Desktop shows its blocking question panel; wait for the user's response, then call `confirm_fact` with exactly that response and rerun `discover`. Use the canonical fact names returned by the service; keep them `confirmed`, not inferred.
5. Inspect the returned DiscoverySnapshot, Feature/Region coverage, provider statuses, and open analysis clarifications. Ask only the process adapter's returned missing analysis facts, using the same wait-then-`confirm_fact` rule. Never continue with a stale DiscoverySnapshot.
6. Call `dfm_analysis` with `plan`. Omitted process selection keeps the project's
   current process; a new project defaults to the compatible `injection` adapter
   and published `injection.default@1.1.0` Snapshot Schema 2 ontology/rule snapshot. The current publication
   contains only main-wall thickness and draft-angle Checks. Die casting currently exposes only its
   approved topology gate. Inspect the returned process, scope version, input hashes, operations,
   ontology snapshot ID/hash, DiscoverySnapshot reference, RuleBindings, and parameter provenance. Explain blocked checks and assumptions before
   execution.
7. The external Analysis Situs/OCCT engine is currently an objective, experimental backend. Request `verification_level=experimental` explicitly in `plan`, then call `start` only when that exact plan reports capability `available`. A certified request must stay blocked and must never silently downgrade. Preserve the returned `run_id`.
8. `start` is non-blocking. Immediately save the returned `run_id` and pass that exact ID to every subsequent `status`, `result`, or `cancel` call; never omit it or invent a replacement. The run publishes background stage, percentage,
   heartbeat, and incremental artifact updates to supported clients. Return
   control to the user after starting; do not spend Agent turns on terminal
   sleep loops or rapid status polling. Use `status` when the user asks, after
   reconnecting, or after a meaningful external wait. Use `cancel` when
   requested. Call `result` only after `succeeded`.
9. Summarize Findings with measurement, rule, evidence, confidence, backend quality, and artifact path. State unresolved checks separately. Present `dfm_report.pptx`, when available, as the primary human-readable report for either geometry backend; retain JSON and Markdown as traceable engineering artifacts. Do not ask the model to recreate the deterministic PPTX.
10. Call `dfm_analysis` with `action=context` and a returned `check_id` only when
   you need to explain that Check, its Factors, or its candidate rules. Treat this
   bounded response as the semantic source; do not infer ontology relationships
   from names and do not request the complete ontology when one Check is enough.

## Capability handling

- `dependency_missing` with `geometry_engine_missing`: explain that the configured external `dfm-geometry` executable is absent or cannot be started, suggest `hermes dfm doctor`, and never install or substitute a geometry backend automatically. Optional reporting dependencies must be diagnosed separately.
- `not_implemented` or `unsupported_capability`: state the limitation and offer supported partial analysis.
- `disabled`: ask the user to configure and enable the capability in a new session.
- `unhealthy`: preserve project and Run IDs and report diagnostics.

The connected geometry backend is the separately built Analysis Situs/OCCT
`dfm-geometry` executable. Its current calculator contract is experimental, not
certified. Hermes already uses it for objective STEP preflight, topology,
recognition, and measurements; it must fail explicitly with
`geometry_engine_missing` or another capability error when unavailable. There
is no PythonOCC or NX execution fallback. Discovery currently freezes an honest
whole-model ordinary-region fallback until the external two-stage recognizer
contract is connected; never present that fallback as a detected process
feature. Every objective backend returns Measurements and geometry artifacts;
Hermes alone performs ontology/rule selection, Evaluation, evidence rendering,
Finding, and reporting.
The initial die-casting scope remains a topology gate. Do not run injection
thresholds under a die-casting label. If the
user requests machining, sheet metal, or another process, report
`unsupported_capability` and the supported process list. Parasolid x_t and the
NX path remain deferred and must not be reintroduced as a fallback for the
OCCT STEP milestone.
Drawing-only and Fusion execution remain explicit unavailable capabilities.

## Engineering integrity

- Never invent measurements, thresholds, findings, standards or successful checks.
- Never turn visual/model inference into a confirmed material, unit or pull direction.
- Geometry engine output is objective and experimental. Hermes alone performs
  rules, Evaluation, Finding and reporting.
- Prefer an explicit blocked/unavailable result over a guess.

## Recovery

After interruption, call project status and then run status with the recorded
ID. Inspect diagnostics events/stdout/stderr for failures. Never automatically
start a replacement run after timeout. Before the configured worker timeout,
call `cancel` with `confirm_cancel=true` only when the user explicitly asked to
stop that run. Never cancel, terminate the native PID, or start a replacement
solely because progress has not changed.
