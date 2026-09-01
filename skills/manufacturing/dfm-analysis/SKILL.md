---
name: dfm-analysis
description: Use when analyzing injection-molded or die-cast part manufacturability from STEP/STP CAD, reserved Parasolid x_t input, PDF engineering drawings, or PNG/JPG drawing images with the built-in dfm_project and dfm_analysis tools.
license: MIT
metadata:
  hermes:
    tags: [DFM, manufacturing, STEP, CAD, engineering-drawing]
    requires_toolsets: [dfm]
---

# DFM Analysis

Manage every analysis as a durable DFM project. Treat tools as the source of engineering facts; use conversation to clarify intent and explain evidence.

## Workflow

The control boundary is
`Agent -> discover/OCR -> Agent interpretation -> validated persistence -> plan -> start`.
Discovery and planning are deterministic service operations. The Agent must
inspect the frozen DiscoverySnapshot, persisted analysis plan, and capability
before deciding whether execution is valid. Starting a plan does not bypass
those decisions or return control to the Agent Loop from inside the worker.

1. Call `dfm_project` with `create`, unless continuing a known `project_id`.
2. Call `dfm_project` with `add_input` for every STEP/STP, reserved Parasolid x_t, or drawing `@file:` reference. When the external OCCT executable is available, STEP intake may return a `viewer_manifest`; Desktop opens it in the 3D viewer. An accepted x_t intake does not mean its geometry reader is available; inspect capability before planning. NX/Parasolid development is deferred and is not the production path for the current milestone.
3. Call project `status`. Inspect the input mode and every analyzer `capability`.
4. Pass `process=injection` or `process=die_casting` when the user has selected it; never infer the process from part shape. Call `dfm_analysis` with `discover`.
   - If it returns `agent_interpretation_required`, call `drawing_context` for each pending drawing input. Interpret only facts explicitly present in the returned OCR fragments using the **current Hermes conversation model**. Do not call a second model endpoint. Submit structured proposals with `submit_observations`, the exact `fragment_id` evidence, and the `expected_revision` returned by the latest context. Submit an empty array when the drawing contains no explicit supported fact. The service owns IDs, status, source policy, validation, and persistence. Rerun `discover`.
   - If it returns `agent_fusion_required`, call `fusion_context`. Use only the returned Observation, Feature, and Region IDs. Submit defensible semantic target proposals with `submit_fusion_links`, or an empty array when the drawing does not establish a target. The service validates geometry relationships and derives candidate/ambiguous status; the Agent never confirms a FusionLink. Rerun `discover`.
   - If it returns `status=clarification_required`, it is a hard stop: do **not** answer the questions yourself and do **not** call `confirm_fact` in the same turn. Call the Hermes `clarify` tool for each open question so Desktop shows its blocking question panel; wait for the user's response, then call `confirm_fact` with exactly that response and rerun `discover`. Use the canonical fact names returned by the service; keep them `confirmed`, not inferred.
5. Inspect the returned DiscoverySnapshot, Observation/FusionLink status, Feature/Region coverage, provider statuses, and open analysis clarifications. Ask only the process adapter's returned missing analysis facts, using the same wait-then-`confirm_fact` rule. Never continue with a stale DiscoverySnapshot.
6. Call `dfm_analysis` with `plan`. Omitted process selection keeps the project's
   current process; a new project defaults to the compatible `injection` adapter
   and published `ontology.injection.default@1.2.0` Snapshot Schema 2 ontology/rule snapshot. The current publication
   contains only main-wall thickness and draft-angle Checks. Die casting currently exposes only its
   approved topology gate. Inspect the returned process, scope version, input hashes, operations,
   ontology snapshot ID/hash, DiscoverySnapshot reference, RuleBindings, and parameter provenance. Explain blocked checks and assumptions before
   execution. Pass `analyzer_key=occt_cpp` only when the external experimental OCCT C++ path is intentionally selected; omit it to retain the PythonOCC STEP reference path. Material and pull direction remain user-confirmed facts—never substitute ABS or `+Z` for a missing answer.
7. Call `start` only when the selected capability is `available`. Preserve its `run_id`.
8. `start` is non-blocking. Immediately save the returned `run_id` and pass that exact ID to every subsequent `status` or `result` call; never omit it or invent a replacement. The run publishes background stage, percentage,
   heartbeat, and incremental artifact updates to supported clients. Return
   control to the user after starting; do not spend Agent turns on terminal
   sleep loops or rapid status polling. Use `status` when the user asks, after
   reconnecting, or after a meaningful external wait. The Agent must never
   cancel a DFM run; cancellation is reserved for an explicit user-interface
   action. Call `result` only after `succeeded`.
9. Summarize Findings with measurement, rule, evidence, confidence, backend quality, and artifact path. State unresolved checks separately. Present `dfm_report.pptx`, when available, as the primary human-readable report for either geometry backend; retain JSON and Markdown as traceable engineering artifacts. Do not ask the model to recreate the deterministic PPTX.
10. Call `dfm_analysis` with `action=context` and a returned `check_id` only when
   you need to explain that Check, its Factors, or its candidate rules. Treat this
   bounded response as the semantic source; do not infer ontology relationships
   from names and do not request the complete ontology when one Check is enough.

## Capability handling

- `dependency_missing`: explain the missing backend (the current PythonOCC/VTK reference worker, the external `dfm-geometry` executable, or optional python-pptx reporting) and suggest `hermes dfm doctor`; never install automatically.
- `not_implemented` or `unsupported_capability`: state the limitation and offer supported partial analysis.
- `disabled`: ask the user to configure and enable the capability in a new session.
- `unhealthy`: preserve project and Run IDs and report diagnostics.

PythonOCC STEP geometry is a non-certified reference backend and remains
available. The separately developed `dfm-geometry` executable is connected as
an `experimental` OCCT C++ Objective backend through versioned contracts; it
must not be presented as certified production capability, and it must never
silently fall back to PythonOCC. Every objective backend must return
Measurement, ScalarField, RenderScene, and TopologyMap; Hermes alone performs
Evaluation, evidence rendering, Finding, and reporting.
The initial die-casting scope remains a topology gate. Do not run injection
thresholds under a die-casting label. If the
user requests machining, sheet metal, or another process, report
`unsupported_capability` and the supported process list. Parasolid x_t remains
an explicit reserved capability; the NX path is deferred and must not block the
OCCT C++ STEP milestone.
Drawing OCR and Agent interpretation run during Discovery. Drawing-only
objective geometry execution remains unavailable; mixed projects route
objective calculations to the configured geometry backend.

## Engineering integrity

- Never invent measurements, thresholds, risk scores, Findings, or successful checks.
- Never invent engineering standards, standard codes, drawing requirements, or
  claim that the default wall/draft scope is a customer or regulatory standard.
- Never convert visual impression or model inference into a confirmed engineering fact.
- Never submit an Observation without exact OCR fragment evidence, or a
  FusionLink with identifiers absent from `fusion_context`.
- Never claim a STEP-only check ran against drawing-only input.
- Never treat a technical test artifact as a production DFM conclusion.
- Prefer explicit unavailable or blocked results over guesses.

## Recovery

After interruption, call project `status`, then run `status` with the recorded IDs. Do not create a replacement project or duplicate Run unless the user requests a new revision.

For a failed or slow run, inspect the `diagnostics.events`,
`diagnostics.stdout`, and `diagnostics.stderr` paths returned by run status.
Partial artifacts remain attached to the Run even when it times out. Never
automatically start a replacement Run after timeout.
