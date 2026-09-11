<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Testing and Evidence

Heartwood separates deterministic contract tests from resource-qualified model evaluations and live platform validation.
No single layer establishes every property of a deployment.

## Test Layers

| Layer | Establishes |
|---|---|
| Unit and schema tests | Validation, state boundaries, policy, diagnostics, model planning, and serialization |
| Persistence fault tests | Atomic replacement, append-boundary interruption, deterministic recovery, process concurrency, schema migration, symbolic-link rejection, and private permissions |
| Skill supply-chain tests | OpenHands format conformance, deterministic complete-tree manifests, signed TUF refresh, offline verification, expiry, rollback and substitution rejection, path confinement, race-resistant copying, exact-digest approval, platform policy, atomic activation, and revocation |
| OpenHands conformance tests | Public typed events, explicit settings, background control, grouped approval, restart recovery, real Task Tracker execution, usage, and sequential specialists with deterministic `TestLLM` |
| Gateway contract tests | Shared command/event behavior, action correlation, projection replay, bounded workspace inspection, coherent REST, WebSocket, and server-sent-events snapshots, transient ordering, credentials, sessions, and imports |
| Interface tests | Terminal, browser, and notebook rendering of gateway-owned status, suggestions, grouped review, files, and changes |
| Container smoke tests | Entrypoint, filesystem, architecture, no-secret image layers, and deterministic OpenHands integration |
| No-network smoke tests | Gateway, OpenHands, grouped action, tool, replay, and audit operation without outbound network |
| Capable-model evaluation | Real Heartwood-managed inference, OpenHands-compatible tool proposal, bounded execution, and exact synthetic output |
| Platform-derived CI | Terra Jupyter inheritance, prefixed internal gateway routing, persistence, image media type, CI-only model rejection as an agent profile, and separate real inference |
| Live synthetic validation | Exact published artifact in Terra or Carina without protected data |

GPU image CI verifies the fully hashed CUDA 12.9 environment, exact vLLM and PyTorch versions, compatibility guards, available tool parsers, launcher, and absence of bundled model weights on standard runners.
Each immutable GPU candidate embeds the complete compatibility matrix for its commit.
Qualification profiles select external model weights and runtime arguments against that candidate; they do not produce profile-specific images.
An optional protected self-hosted GPU job runs the same model qualification used on managed platforms when an eligible runner is configured.
Without GPU hardware, CI does not claim successful CUDA initialization or GPU model loading.

The shared coding-agent acceptance test performs direct model inference and then drives the real Heartwood gateway and OpenHands adapter through structured terminal proposals, grouped approval and rejection, synthetic file modification, byte-exact independent verification, proof that the rejected action did not execute, fresh-process replay, and hash-chain-verified audit export.
It emits a machine-readable qualification record containing the exact runtime, model revision, GPU, driver, context, tensor parallelism, server parser, and agent tool mode.
The CPU capable-model job and GPU qualification wrapper use this same acceptance contract instead of maintaining separate agent scenarios.

The CPU capable-model job obtains its model through the complete portable-transfer contract.
It downloads and verifies the pinned model in a connected project, exports a bundle, mounts only that bundle into an empty project with `--network none`, imports and selects it after explicit license approval, verifies the normal launcher plan, and then runs the shared coding-agent acceptance task against the imported copy.
Deterministic tests additionally cover reproducible bundle bytes, GGUF and vLLM snapshots, untrusted qualification claims, path and symbolic-link attacks, tampered and incomplete payloads, incompatible runtime metadata, insufficient or modified destinations, cancellation cleanup, interrupted retries, duplicate imports, process restart, and the absence of a repository download path for transferred models.

OpenHands SDK conformance tests use the real conversation persistence layer and deterministic `TestLLM`.
They verify that pending actions and completed tool turns survive restart without repeated model or tool work, grouped approval executes each action once, grouped rejection executes none, active work can be steered and paused, a stale running state fails closed as an unknown outcome, persisted progress appears before completion, Task Tracker updates are translated, and one tool-free research-planning specialist returns to its parent conversation.
Structured-outcome conformance tests exercise the upstream finish-tool schema through the same private persistence layer.
Reported success, partial success, blocked, failed, and unknown outcomes survive restart without another model call.
The SDK conversation can be finished for any of these outcomes; neither lifecycle completion nor a model-reported success proves that required artifacts or independent checks passed.
Workspace contract tests qualify the pinned OpenHands Git change and diff APIs, non-Git typed-action fallback, canonical path handling, nested private-state exclusion, traversal and symlink rejection, special and binary files, UTF-8 boundaries, limits, audit scrubbing, and cross-interface transport.
The browser reference analysis stops its gateway, replays and mutates the same session through the CLI, restarts the gateway, and verifies that the browser receives the CLI update on one contiguous authoritative sequence.
Browser tests build the current production assets before starting the preview server, exercise direct and fallback live-update states, scan the rendered interface with axe, and verify keyboard focus, reduced motion, and reflow at desktop, tablet, and narrow notebook widths.
Adversarial response tests cover raw HTML, unsafe links, remote images, invisible control characters, oversized Markdown, heading hierarchy, and keyboard access to scrollable code and diff regions.
Gateway transport tests bound request bodies, reject malformed text, keep API failures out of static-page fallback, and verify browser security headers for direct and Jupyter-proxied origins.

Persistence compatibility fixtures cover every current project, configuration, session, audit, Skill, and OpenHands envelope.
Each fixture must pass the deterministic migration registry and its owning typed loader.
Audit checkpoint tests cover content minimization, canonical encoding, deployment-registry precedence, project isolation, signer authentication, endpoint and file constraints, pinned signer identity, remote-response verification, local-service boundaries, concurrent publication, interrupted publication, wrong-key and content tampering, retention validation, and independently trusted verification.

Skill source tests create an ephemeral Ed25519 TUF repository and use the production Python-TUF client for connected-equivalent and no-network refreshes.
They alter metadata, target archives, manifests, source files, source identities, platform declarations, controlled-data claims, and installed artifacts to verify that each path fails closed.
Gateway, REST, CLI, and browser tests consume the same Skill projection and exact-digest installation contract.

Native packaging CI uses deterministic dependency-tool substitutes to verify failure paths and reproducibility, then installs the release archive in an empty Ubuntu 24.04 AMD64 container and runs the real CPU inference and browser paths.
Actual Terra and Carina qualification still requires the exact published artifact and synthetic task on those platforms because public CI cannot provision their managed workspaces.
That qualification promotes one precise row in the [GPU compatibility matrix](../reference/gpu-compatibility.md); it does not qualify other drivers, model revisions, precisions, parsers, context sizes, or tensor-parallel layouts.

Pull-request validation and main-branch validation both call the shared capable-model acceptance workflow, while protected GPU qualification remains a separate entry point.
Every pull request includes capable-model acceptance in the required `Release Candidate Ready` aggregate so changes cannot bypass the real model, OpenHands, approval, replay, and audit contract.
Registry writes, multi-platform manifest assembly, and moving-tag promotion remain main-only; pull requests build the same image stages and validate the promotion scripts without receiving package-write access.
Release creation also requires the repository-managed Python and JavaScript/TypeScript CodeQL analyses for the exact commit.
Compute-intensive container builds and capable-model acceptance run on appropriately sized Blacksmith runners and reuse bounded GitHub Actions BuildKit caches; short policy and documentation checks remain on standard GitHub runners.

## Research Evaluation Evidence

Research evaluations distinguish connectivity, tool compatibility, workflow completion, artifact completeness, coding correctness, statistical correctness, policy adherence, and recovery.
A successful connection or tool call does not establish research-task quality.

The evaluation contracts record the suite and fixture identity, model revision, provider, runtime, hardware, context, tool parser, Skill digest, harness revision, dated trial, independent checks, and measured usage.
Unavailable token counts and reported cost remain unknown rather than being recorded as zero.
The records have no fields for prompts, tool output, credentials, or participant data.
Metadata still requires review before publication, and a content digest does not prove who produced an evaluation.

The gateway separately observes the session's request model, installed OpenHands version, explicit context limits, selected confirmation mode, platform adapter, and fingerprints of model options and security configuration.
The security fingerprint binds the deployment policy and the actual OpenHands confirmation policy and analyzer configuration.
The driver binds this observation to the trial before model work and checks it again before reviewed continuations and finalization.
Runtime changes leave incomplete evidence rather than combining results from different client configurations.
Injected, deterministic, and unconfigured backends cannot supply live-model qualification; missing runtime identity or explicit context limits also prevent qualification.
Remote model weights, precision, GPU hardware, and server software remain declared metadata and require deployment-side evidence.
The client observation does not attest to those remote properties or verify the declared Skill and harness digests.

The evidence assessor requires the latest three real-model trials of every case to pass all required checks within a configurable freshness window, which defaults to 30 days.
Deterministic test doubles exercise the harness but cannot qualify a model.
Changed configurations, changed suite definitions, unknown model revisions, expired evidence, missing checks, and recent failures cannot inherit earlier successful results.
Three trials are a functional regression gate, not a statistical estimate of clinical or scientific reliability.
An assessment includes its policy, time, exact configuration and suite digests, and source run identifiers; it does not automatically change model recommendations or action-approval policy.

### Parallel Review Comparison

`heartwood.model_policy.parallel_reviews.assess_parallel_reviews` compares matched sequential and parallel trials through the same evidence assessor.
Pure qualification rules live in the policy package so runtime admission can reuse them without importing benchmark execution.
Each case binds the exact advisory specialist identifiers; evidence does not qualify additional roles or a larger worker pool.
Both configurations must pass the complete research checks and additional checks for reviewer scheduling, isolation, lineage, findings, and parent synthesis.
The configurations differ only in requested specialist concurrency; model, context, tools, Skills, specialist definitions, fixtures, seeds, and budgets remain matched.
The gateway observes the actual global OpenHands tool limit and whether its scoped advisory executor is installed, separately from the requested reviewer count.
Both trials retain the same runtime observation and fingerprint; the reviewer scheduling check must establish actual sequential or overlapping execution.
Completed and failed native specialist observations retain a monotonic execution interval through the session journal and shared projection.
The interval covers the native task's execution and cleanup, excluding approval, queuing, and child creation; cancellation before execution has no interval.
Only intervals with the same process-local clock identity can establish overlap.
Task overlap does not establish simultaneous provider requests, useful findings, or successful completion, and interrupted work without a final observation supplies no completed interval.
The gateway also records a fingerprint of executable specialist definitions and supplied Skill metadata.
That fingerprint excludes installation paths and does not replace the separate Skill-tree digest for bundled resource bytes.

The default comparison policy requires three recent trials per case, at least a 10% reduction in both median and total elapsed time, and no more than a 25% increase in total reported cost or tokens.
Unknown usage, missing reviewer checks, changed configurations, and failed trials prevent qualification.
These thresholds are explicit policy parameters, not statistical evidence of general model superiority.
The assessment preserves both source assessments and per-case measurements; it neither authenticates arbitrary evidence files nor grants permission to launch parallel tasks.

`prepare_parallel_review` validates the current route and exact workflow stage against retained qualification evidence.
Its preview binds the project, session, workflow revision, artifact snapshot, reviewers, worker count, and requested budget to the source trial digests and qualification policy.
Requested limits cannot exceed the tested workload's budget; smaller limits remain admission controls, not guaranteed completion or provider-side billing caps.
The preview remains stable while that evidence is unchanged and fresh; a changed route, replaced trial, new failed trial, or expired evidence requires reevaluation.
Supplying its fingerprint checks an exact consent match but does not itself record consent or approve a tool action.
Evidence must come from the deployment's trusted evaluation process, not model output or a researcher-supplied success flag.

Native SDK tests exercise this plan through the workflow journal, grouped approval and rejection, concurrent child startup, assessment, replay, and reopening without new model calls.
Failure-path tests cover changed evidence, expired consent, cancellation during qualification, lost ownership, duplicate admission, and interruption at each paired-append boundary.
These deterministic tests establish protocol behavior, not capable-model quality, speed, or platform qualification.

### Experimental Review Trials

Qualification requires measurements, but a first benchmark cannot already be qualified.
`ReservedReviewTrial` therefore prepares explicitly experimental work from one incomplete `EvaluationStore` record, with a closed `qualification-trial` plan type distinct from a qualified recommendation.
The harness verifies pinned synthetic inputs before reserving the record and uses the owning workflow's clock for its start time.
The plan binds the reserved trial, case, seed, session, configuration, observed runtime, reviewers, limits, and expiry.
Each preview and dispatch rereads the reservation and observes the owning backend; missing, completed, corrupt, expired, or changed reservations cannot start work.
The runtime observer must not acquire gateway or native agent-step locks while the parent is awaiting dispatch.

Experimental trials use the same workflow consent, grouped tool approval, paired journal, cancellation, and restart rules as qualified reviews.
The shared interfaces label them **Experimental parallel review**; no previous passing results are fabricated to obtain concurrency.
Narrower consented limits apply to the stage's remaining work, not a fresh allowance for each specialist.
Admission does not produce a passing evaluation result: the benchmark must separately record and independently assess execution, findings, synthesis, usage, latency, replay, and audit evidence.

### Synthetic Research Cases

The maintained research fixtures cover dataset readiness, a held-out linear baseline, and independent result verification.
Their identities bind the synthetic input bytes, task instructions, expected artifact names, and required checks.
The readiness case contains missing values, invalid values, duplicate observations, and an outcome-derived column that must not be used as a predictor.
The baseline uses a fixed subject-disjoint split, independently calculated predictions and metrics, a training-mean comparator, and whole-subject omission sensitivity checks.

Artifact verification checks structured outputs rather than accepting the agent's completion message.
A parseable script and correct reported metrics do not establish that the program ran or reproduced the result.
Tool execution, independent re-execution, action approval, fresh-process replay, and audit verification require separate evidence from their respective owners.
These small fixtures test software behavior, not scientific generalizability.

The shared research driver accepts a dedicated project containing only the pinned synthetic inputs and explicitly supplied verification artifacts.
It uses normal gateway commands and requires a review callback for every pending action group; it does not execute generated programs directly or implicitly approve proposals.
The baseline's reproduction step requests a separate, reviewed terminal execution of the unchanged program and checks its regenerated outputs against the originals.
The independent verification case distinguishes a truthful byte comparison from evidence that the program actually ran.
Reproduction evidence requires a previously absent output directory and captures the outputs after a separately approved command, before approving subsequent actions.
It binds the original program, fixture inputs, and primary outputs before approval and after execution.
A no-op command followed or preceded by copied outputs does not satisfy this check.
The shared reproduction contract records the session, run, stage, action, exact shell-quoted invocation, and inspected file hashes without retaining file content.
Preparation is not execution evidence or permission.
The first observed outcome must be an approved successful action with all protected files unchanged and every expected output available; failed evidence cannot be repaired by copying files later.
Restoring a serialized record does not independently establish execution: the session owner must supply its trusted ordering and approval history.
Workflow journal tests reject missing, repeated, reordered, cross-session, denied, grouped, failed, or wrong-directory execution sources.
Interruption tests cover persisted tool completion without a reproduction observation and ensure duplicate callbacks, reconciliation, and restart cannot capture replacement evidence or repeat work.
The baseline conformance test uses real OpenHands tools with deterministic `TestLLM` for plan review, analysis execution, separately approved reproduction, independent checks, reporting, restart, and content-minimized audit export.
It establishes orchestration behavior, not capable-model research performance.
These bounded observations are not a sandbox or proof against an adversarial process changing and restoring files between observations.
Fresh-process checks compare the gateway's persisted projection, event-chain identity, and verified audit identity without creating a model client.

The driver reserves a private result under `.heartwood/evaluations/` before starting model work.
Before reviewing a pending group or assessing completion, it waits for the SDK worker's final publication and reconciles usage through the shared gateway.
An early finished lifecycle cannot by itself trigger reproduction or finalize an evaluation.
If finalization does not settle within the bounded wait, the trial remains incomplete.
An interrupted evaluator leaves an incomplete trial with unverified checks, rather than silently removing an unsuccessful attempt from the evidence.
Successful evaluation replaces that record atomically; completed evidence cannot be overwritten with different results.
The result loader includes incomplete trials and rejects corrupt or substituted records.
Loading results does not resume a session or repeat a model call.
An incomplete record's finish timestamp is its reservation time, not an estimate of when the process stopped.
These reports do not replace authoritative session events or signed audit checkpoints.

Evaluation records include observed time, action, model-call, token, and reported-cost budgets.
Limits are checked between gateway updates and after review, before admitting another action; a request already in flight may finish before pause takes effect.
Durable provider usage is reported at SDK run boundaries, so these observed limits do not cap every request inside an uninterrupted agent run.
Finishing exactly at a provider limit is allowed, but reaching that limit prevents another reviewed continuation or benchmark turn.
Action limits count proposals, including unsuccessful or rejected actions; a group already counted at the limit may execute after review, but an oversized group cannot.
The evidence assessor independently rejects measured overruns even when the reported task checks passed.
Elapsed task time includes researcher review and reproduction, but excludes the subsequent audit export and fresh-process replay checks.
Unavailable usage remains unknown, including a zero value when the gateway cannot distinguish an unpriced call from a genuinely free call.
Use provider-side spending limits as an additional control for hosted evaluations.
Run generated code in an appropriately isolated synthetic environment and inspect complete action groups before approving them.

### Action-Risk Evaluation

The action-risk corpus contains benign, ambiguous, destructive, encoded, injected, and network-capable proposals for the terminal and file editor.
Evaluation constructs typed OpenHands actions and calls the same analyzer and confirmation-policy factory used by Heartwood conversations; it never executes the proposals.
Each case supplies either an optimistic low-risk model label or an unknown label.
OpenHands' LLM analyzer consumes that label rather than making an independent model call, so this evaluation measures the analyzer ensemble, not a model's ability to classify risk.

Reports retain case identities, the corpus and analyzer configuration digests, SDK version, evaluation date, decisions, and latency without retaining commands or file contents.
False approvals, unnecessary confirmations, and unknown classifications remain separate measurements.
A deployment assessment requires complete matching evidence, no false approvals, confirmation of unknown classifications, and explicit limits for unnecessary confirmations and latency.
Passing a synthetic corpus is necessary evidence for a policy decision, not proof that arbitrary actions are safe.
Unrecognized commands and misleading model labels can escape static analysis; Review Every Action remains the safe fallback, and benchmark results never change the selected policy automatically.

## Synthetic Data Rule

Source control, public examples, CI, screenshots, and replay fixtures use synthetic data only.
Protected health information must never enter a test fixture, public log, screenshot, pull request, or model-evaluation artifact.

## Claims

- **Implemented** means code and automated contract tests exist.
- **CI validated** means the behavior ran in the documented automated environment.
- **Live synthetic validated** means the published artifact ran in the named platform with synthetic data.
- **Institution approved** requires separate institutional evidence and is never inferred from the previous levels.

Release documentation states the supported contract rather than preserving individual validation transcripts.
