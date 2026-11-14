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

Outside-operation conformance tests use the native SDK MCP client against an authenticated synthetic MCP endpoint on loopback.
The maintained token verifier checks signature, issuer, audience, and expiry; the service separately checks project, researcher, worker attempt, policy, exact request, and input bytes.
Its synthetic record and operation identity are committed in one SQLite transaction with the current service authorization, including when a request waits behind revocation.
Concurrent replays cannot create another record or reuse a decision for a different operation.
Tests discard a reply after the effect commits, restart the gateway and synthetic service, rotate the test credential, and recover through status lookup without resubmission.
These checks establish native protocol and authorization behavior; they do not establish process isolation, deployed network policy, protected input storage, or controlled-data readiness.

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
Capable-model acceptance runs on the organization's large hosted runner because CPU inference does not finish inside its command deadline on a standard runner; every other job runs on standard GitHub-hosted runners, and container builds reclaim unused preinstalled toolchains first and reuse bounded GitHub Actions BuildKit caches.

## Native Worker Dependency Candidates

The native worker qualification path needs SDK admission and remote-event hooks that are not available in the public application lock.
`deploy/build-worker-candidate.py` builds a separate, explicitly versioned wheel set from immutable upstream source archives and reviewed patches in `images/worker/candidate/`.
It verifies source, patch and build-tool hashes, uses a fixed source timestamp, preserves upstream licenses, and records each wheel digest.
The candidate workflow repeats the build and requires byte-identical wheel records.
Building these artifacts supports integration tests; it does not replace the application lock, install dependencies, publish upstream packages, or establish a supported deployment.

```bash
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --output test-results/worker-candidate
```

The builder requires the uv version recorded in the source manifest and a new output directory.
The conversation patch adds bounded remote-event admission, terminal failure propagation and explicit outside client-tool control.
The sandbox patch exposes the native configuration-admission record needed to bind a worker to its admitted policy and process generation.
The dependency patch updates the SDK's stale inference-client constraint, public imports and usage-cost handling after its MCP 2 migration.
The local package version suffix distinguishes these reviewed candidates from upstream releases.
Source revisions, patch digests and wheel records must accompany any qualification result that uses them.
Promotion into the production dependency lock requires compatible upstream releases or an explicitly maintained patched dependency source, followed by integrated qualification of the exact resulting image.

The candidate environment command compiles a separate hash-pinned dependency graph and installs it only into a new environment.
It fixes the Python and package-manager versions, runtime hashes and isolated build dependencies, and checks installed-package consistency.
The fixed release cutoff has explicit exceptions for the file-lock security update and the inference-client version required by the reviewed SDK patch.
The lock includes test tools.
The application builder archives the current Heartwood commit and its pinned Skill submodule, then builds wheels from that snapshot.
Only the temporary Heartwood package metadata receives the declared candidate dependency pins and local version suffix; source checkouts and public dependency metadata are unchanged.
The pinned Skill catalog is built without source or metadata changes.
The application record includes both source commits, wheel hashes and a hash-pinned installation manifest.
Installation uses those local wheels without dependency resolution, then verifies that their declared dependencies are satisfied by the locked environment.
Application files are copied rather than hard-linked to the package cache so bundled Skills retain the independent-file integrity requirement.

```bash
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --application --output test-results/application-candidate
bash images/worker/candidate/runtime.sh compile test-results/worker-candidate
bash images/worker/candidate/runtime.sh install test-results/worker-candidate test-results/application-candidate test-results/worker-environment
```

Set `UV_BIN` when the pinned package manager is not on `PATH`.
An existing environment is rejected to preserve installed work.
The candidate workflow verifies lock regeneration and reproducible application builds, installs this environment and runs the Python suite with isolated imports against the packaged application and SDKs.
This verifies package integration, not operating-system isolation or model-route eligibility; native deployment drills remain required.

### Candidate Images

The shared Dockerfile accepts an explicit `candidate` dependency profile for generic CPU qualification images.
Supply the verified SDK artifacts under `sdk/` and application artifacts under `application/` in a separate named build context.
The build installs them through the same candidate environment command, checks the application source commit against the image revision, and retains both build records in `/opt/heartwood/qualification/`.
The normal locked profile remains the default.

```bash
docker buildx build --load --file images/Dockerfile --target worker-image \
  --build-arg HEARTWOOD_DEPENDENCY_PROFILE=candidate \
  --build-arg HEARTWOOD_IMAGE_FLAVOR=worker \
  --build-arg HEARTWOOD_REVISION="$(git rev-parse HEAD)" \
  --build-context worker-qualification-artifacts=test-results/image-candidate \
  --tag heartwood-worker:qualification .
```

Use `runtime-image` with the same inputs for the controller application.
Both targets retain the shared non-root runtime and browser build.
The worker target retains the fixed launch restrictions.
The browser bundle is installed beside the candidate CLI from the existing frontend build; fixture data is included in the CLI wheel.
Missing CLI resources resolve only within the installed package or the exact editable checkout layout, never relative to the research project.
Image construction and worker smoke checks do not establish native sandbox, provider-authorization or sensitive-data readiness.

### Gateway Build

The gateway candidate uses the pinned upstream source and focused Docker provisioning patch recorded in `images/worker/candidate/gateway.json`.
Build it with the existing candidate builder:

```bash
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py \
  --gateway --output test-results/gateway-candidate --cache test-results/gateway-cache
```

The output directory must be new and separate from the reusable cache directory.
The builder verifies source, patch, CLI and build-tool archives, stamps a distinct local workspace version without changing registry dependencies, and uses locked Cargo dependencies inside a resource-bounded Linux container.
Rust, CMake and image inputs are pinned; no mutable system-package installation is required.
Cached archives are reverified before extraction.
The resulting binary is packaged with the unchanged upstream gateway Dockerfile and checked for its expected version.
The output retains binaries, the upstream license, packaging inputs, and a record binding source, manifest, lockfile, binary and image identities.
These records establish build provenance; they do not claim byte-identical gateway builds or qualify untested architectures.

The same manifest pins the supervisor, sandbox and Docker client image references used by native qualification.
`images/worker/candidate/controller.Dockerfile` adds the verified gateway tools and pinned Docker client to the shared candidate runtime image.
Supply `RUNTIME_IMAGE` and `DOCKER_CLIENT_IMAGE` as build arguments and the gateway output as the `worker-qualification-tools` named context.
The controller remains outside the worker sandbox and needs an explicitly supplied Docker socket for native integration tests; the worker never receives that socket.
Gateway artifacts and upstream patches are qualification inputs, not published upstream releases or a supported production dependency channel.

### Installed Native Integration

The maintained suite includes a synthetic session whose explicitly approved terminal action attempts to read control credentials, alter protected state and read-only projections, and reach a forbidden provider path.
The outside fixture checks actual state canaries and provider traffic before and after the attempt; worker-reported success cannot replace those observations.
The allowed synthetic endpoint redirects a request to a forbidden path; qualification requires observing the redirect request outside the worker and no request at its destination.
The same POST-only profile rejects an unapproved WebSocket upgrade; this does not qualify a separately enabled WebSocket endpoint or its message rules.
Tool execution must succeed independently of the scripted conversation's completion status.
The installed suite runs direct IPv4, mapped IPv6, native IPv6, TCP and UDP probes against controlled listeners on a disposable dual-stack Docker network.
A separate outside client must reach each listener before and after the worker attempt; timeouts and unreachable destinations cannot pass as policy denials.
The supervisor retains its supported bridge configuration, and workers retain `network=none`; the additional network connects only the trusted controller namespace and its outside client.
Injected instructions appear in synthetic input and tool output, but deterministic provider responses do not establish resistance to model-driven prompt injection.

The candidate workflow builds the gateway and dependency wheels independently, then assembles the shared worker and runtime images for native integration.
Run the same tests locally against those images and gateway artifacts:

```bash
python3 deploy/qualify-native-worker.py \
  --worker-image heartwood-worker:qualification \
  --runtime-image heartwood-runtime:qualification \
  --gateway-artifacts test-results/gateway-candidate \
  --output test-results/native-qualification
```

The command requires a local Linux Docker engine and a new output directory.
It verifies gateway artifact digests, builds the outside controller, and runs the installed application without source overlays.
The disposable gateway shares the controller's network namespace without publishing host ports; each worker still uses the native sandbox's isolated network.
Only the outside controller and gateway receive the Docker management socket.
The synthetic TLS provider uses a generated test credential and a fixed tool-call sequence; no external inference service or API key is needed.

The workflow test uses Heartwood's production native launcher, independently admitted policy, read-only inputs and Skills, exact outside approvals, aggregate suppression, verified audit, and revocation before writer release.
After shutdown it checks protected-state replay in a fresh process and rejects incorrect project identity and corrupted audit data.
A separate fault test discards a successful native create response and requires a fenced attempt until outside revocation removes its containers and volumes.
Test failure or failed cleanup prevents qualification; retained evidence must include the successful command exit and teardown results, not merely an individual assertion record.
If revocation cannot settle, the local fixture retains its control state for reconciliation.
CI retains only the selected synthetic evidence and logs, excluding private gateway state and generated credentials.
These tests do not establish model-driven attack resistance, signed-checkpoint retention, tenant authorization, or institutional sensitive-data approval.

### Native Network Qualification

`deploy/tests/worker_network_qualification.py` runs against an existing disposable sandbox through the native authenticated control API.
Run it on a trusted Linux controller with IPv4 and native IPv6 addresses that reach its canary listeners; account for any container port mappings when supplying the addresses and ports.
The controller needs the candidate SDK, its gateway client certificate, and network reachability to the gateway.
The admitted policy must allow the hostname supplied through `--allowed-host` (default `host.docker.internal`) while denying the canary ports and unlisted destinations.
The worker receives only the probe program and synthetic destination addresses, never the controller certificate or key.

```bash
python deploy/tests/worker_network_qualification.py \
  --gateway "$GATEWAY_ADDRESS" --ca "$CA_PATH" --cert "$CLIENT_CERT_PATH" --key "$CLIENT_KEY_PATH" \
  --sandbox "$SYNTHETIC_SANDBOX" --ipv4 "$CONTROLLER_IPV4" --ipv6 "$CONTROLLER_IPV6" \
  --port 19672 --ipv6-port 19673
```

The check opens TCP and UDP canaries and independently reaches every tested transport before and after the worker probe.
It requires explicit policy denial for direct IPv4, mapped IPv6, native IPv6, UDP, and an unlisted hostname; timeout, refused connection, and missing route do not qualify as denials.
Unknown names may resolve to observation-only synthetic addresses without gaining connection permission.
Any canary traffic from the probe, listener failure, or change in sandbox identity, process generation, or admitted configuration invalidates the result.
The record binds the probe digest and observed native sandbox identity; retain it with the image and gateway build provenance.
This command does not create or delete the sandbox, change its policy, qualify IPv6 inference, or establish model authorization or sensitive-data readiness.

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
The maintained plan-review suite pairs a prohibited outcome-derived predictor with a valid-plan control over the same pinned synthetic data.
Its finding scorer reuses the workflow's independent baseline-plan checks, requires the exact fixture snapshot and selected reviewers, and rejects missed defects and false accusations.
This finding check alone does not establish scheduling, provider execution, isolation, parent synthesis, recovery, or route qualification.
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
The harness verifies pinned synthetic inputs before reserving the record and uses UTC timestamps at the same second precision as workflow admission.
The plan binds the reserved trial, case, seed, session, configuration, observed runtime, reviewers, limits, and expiry.
Each preview and dispatch rereads the reservation and observes the owning backend; missing, completed, corrupt, expired, or changed reservations cannot start work.
`SessionGateway.bind_evaluation_observer` binds a read-only observer to the current owned service without acquiring gateway or native agent-step locks during dispatch.
Closing or replacing that service revokes the observer; reading runtime metadata never acquires ownership or authorizes work.

Experimental trials use the same workflow consent, grouped tool approval, paired journal, cancellation, and restart rules as qualified reviews.
The shared interfaces label them **Experimental parallel review**; no previous passing results are fabricated to obtain concurrency.
Narrower consented limits apply to the stage's remaining work, not a fresh allowance for each specialist.
Admission does not produce a passing evaluation result: the benchmark must separately record and independently assess execution, findings, synthesis, usage, latency, replay, and audit evidence.

`reserve_planning_review_trial` and `run_planning_review_trial` run retained comparisons through the same gateway workflow controls and explicit approval callback as researcher interfaces.
The caller prepares an isolated synthetic project with `data.csv`, `dictionary.json`, and a single output directory containing `plan.json`, and completes planning before reserving the review.
The workflow question and files must match one maintained `planning_review_tasks()` case; unrelated files, substituted inputs, changed runtimes, and already reviewed stages are rejected.
Planning work is excluded from review usage and latency.
Reservation waiting consumes the execution deadline but is excluded from measured review latency; unavailable provider counters remain unknown.
The runner records sequential or overlapping native task intervals, exact reviewer lineage, independent findings, unchanged input files, grouped authorization, a settled parent outcome, and fresh-process replay with audit verification.
The isolation check covers the observed advisory tool scope and supplied files, not operating-system or network sandbox isolation.
The synthesis check establishes a structured successful parent outcome after the reviewers settle, not the correctness of unrestricted narrative claims.
Stopped or rejected trials retain failed checks; interrupted trials remain incomplete rather than being retried automatically.
Neither a successful deterministic trial nor a completed record automatically qualifies or promotes a provider route.

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
Independent Result Verification reuses this execution witness for a fixed Python metadata probe and the subsequent analysis invocation.
The gateway binds one absolute interpreter; both commands use isolated mode, and the analysis invocation first rechecks the protected environment record.
The environment check compares required package identities and versions using the standard Python packaging rules.
Matching model-written metadata without an approved probe remains unverified.
Conformance tests cover real tool execution, incompatible versions, fresh-output confinement, preserved experiment fingerprints, interface capture, restart, and content-minimized audit export.
These bounded observations are not a sandbox or proof against an adversarial process changing and restoring files between observations.
Fresh-process checks compare the gateway's persisted projection, event-chain identity, and verified audit identity without creating a model client.
Managed deployments pass the operator-bound protected-state directory and project identity to that verifier; project content cannot select the replay source.

The driver reserves a private result in the bound state's `evaluations/` directory before starting model work (`.heartwood/evaluations/` for local projects).
Before reviewing a pending group or assessing completion, it waits for the SDK worker's final publication and reconciles usage through the shared gateway.
An early finished lifecycle cannot by itself trigger reproduction or finalize an evaluation.
Workflow completion additionally requires passing artifact checks and, where required, independently witnessed reproduction; a final message alone is not evidence of completion.
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
