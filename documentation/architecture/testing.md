<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Testing and Evidence

Heartwood's claims rest on four evidence levels, and each test layer reaches a stated level.
No single layer establishes every property of a deployment.

## Claims

- **Implemented** means code and automated contract tests exist.
- **CI validated** means the behavior ran in the documented automated environment.
- **Live synthetic validated** means the published artifact ran in the named platform with synthetic data.
- **Institution approved** requires separate institutional evidence and is never inferred from the previous levels.

Native qualification is CI validation of the managed worker boundary with synthetic workloads.
It does not establish sensitive-data readiness, and a qualification record is not institutional approval.
Release documentation states the supported contract; run-specific results stay in CI artifacts and pull requests.

## Test Layers

| Layer | Runs as | Establishes |
|---|---|---|
| Unit, schema, and persistence | `pytest` in the Python workflow | Validation, policy, diagnostics, serialization, atomic replacement, append interruption and recovery, process concurrency, migrations, and private permissions |
| OpenHands conformance | `pytest` with the real SDK and deterministic `TestLLM` | Typed events, grouped approval and rejection, restart without repeated model or tool work, Task Tracker, structured outcomes, and sequential specialists |
| Gateway and interface contracts | `pytest`, the Web UI workflow | Shared command and projection behavior, review modes, outside-operation phases, workspace inspection, transports, ingress, and terminal, browser, and notebook rendering |
| Supply chain | `pytest` with an ephemeral TUF repository | Skill refresh, offline verification, expiry, rollback, substitution, path confinement, exact-digest approval, and revocation |
| Container and native smoke | Container smoke and native release workflows | Entrypoints, filesystem layout, no secrets in image layers, offline operation, and installation of the release archive in an empty Ubuntu 24.04 container |
| Capable-model acceptance | Capable model workflow, optional GPU qualification | Real inference driving the gateway and OpenHands through approval, rejection, replay, and audit export |
| Native qualification | Worker candidate workflow | The installed application, worker image, and qualification gateway across the complete managed worker boundary |
| Live synthetic validation | Operator run in Terra or Carina | The exact published artifact in the named platform without protected data |

Deterministic layers use fake providers for failure paths and `TestLLM` for SDK conformance; capable models appear only in the acceptance path that needs real inference.
Browser tests build production assets, scan the rendered interface with axe, and check keyboard focus, reduced motion, and reflow at desktop, tablet, and narrow notebook widths.

Outside-operation conformance uses the native SDK MCP client against an authenticated synthetic MCP service on loopback.
The service verifies the token's signature, issuer, audience, and expiry, and separately checks project, researcher, worker attempt, policy, exact request, and input bytes; its record and operation identity are committed in one SQLite transaction.
Tests discard a reply after the effect commits, restart the gateway and service, rotate the credential, and recover through status lookup without resubmission.
These tests establish protocol and authorization behavior, not process isolation or deployed network policy.

## Continuous Integration

Pull-request and main-branch validation both end in the required `Release Candidate Ready` aggregate, which requires every validation workflow, including capable-model acceptance; on pull requests it also requires native worker qualification.
On a pull request, native qualification and its candidate builds are skipped only when every changed path is under `documentation/` or `.github/` other than the worker candidate and pull-request validation workflows; any other path, including one the selection does not recognize, requires them.
On the main branch, the separate **Native Qualification** workflow runs the complete native qualification for every commit and attests its evidence; it has no concurrency group, so a newer commit never cancels the run of an earlier one.

Pull-request jobs that build and run candidate code hold no signing or package-write permission; the Python job holds an identity token only to upload coverage to Codecov.
Multi-platform manifests, moving tags, and evidence attestation run only on the main branch.
Registry writes run on the main branch, except that the manual Terra GPU Qualification Candidate workflow pushes a `candidate-sha-<commit>-terra-gpu-nvidia` image from the branch it is started on.
Release creation requires `Release Candidate Ready`, the `Analyze (python)` and `Analyze (javascript-typescript)` code-scanning checks, and the attested Native Qualification evidence for the exact commit.

## Capable-Model Acceptance

One coding-agent acceptance contract drives the real gateway and OpenHands adapter through structured terminal proposals, grouped approval and rejection, a synthetic file change, byte-exact independent verification, proof that the rejected action did not run, fresh-process replay, and hash-chain-verified audit export.
It writes a machine-readable record of the runtime, model revision, hardware, context, tensor parallelism, parser, and tool mode.

The CPU job runs on the `heartwood-ubuntu-large` runner and obtains its model through the portable-transfer contract: it downloads and verifies the pinned model, exports a bundle, imports it into an empty project in a container with `--network none` after explicit license approval, and runs the acceptance task against the imported copy.
The GPU image workflow verifies the hashed CUDA environment, vLLM and PyTorch versions, parsers, and the absence of model weights on standard runners; it does not claim CUDA initialization or GPU model loading.
The protected self-hosted GPU workflow runs the same acceptance contract with external weights and promotes exactly one row of the [GPU compatibility matrix](../reference/gpu-compatibility.md); it does not qualify other drivers, model revisions, precisions, parsers, context sizes, or tensor-parallel layouts.

## Candidate Builds

The default lock installs the patched SDK wheels from `vendor/runtime-wheels/`, which is not committed.
`deploy/build-worker-candidate.py` builds them from immutable upstream archives and the patches in `images/worker/candidate/`, verifying source, patch, and build-tool hashes and using a fixed source timestamp.
The patches add bounded remote-event admission and outside client-tool control (conversation), explicit environment replacement for terminal backends (terminal), the configuration-admission record that binds a sandbox to its policy and process generation (sandbox), and a security-fixed file-lock requirement (dependency).
A local version suffix such as `+heartwood.3` marks a patched build; the recorded upstream source revision, not the version number, identifies the upstream code.

```bash
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --bootstrap
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --output test-results/worker-candidate
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --application --output test-results/application-candidate
```

`--bootstrap` builds the wheels into `vendor/runtime-wheels/` when the directory is absent, and otherwise verifies the wheels and their build record against `sources.json`.
The other commands require a new output directory; `--application` archives the current commit with its pinned Skill submodule and records both commits, wheel hashes, and a hash-pinned installation manifest.
The worker candidate workflow builds each twice and requires identical build records, checks that `runtime.sh compile` reproduces the committed candidate lock, installs it with `runtime.sh install`, and runs the Python suite with isolated imports against the packaged application.

The worker and runtime images use the shared `images/Dockerfile` with `HEARTWOOD_DEPENDENCY_PROFILE=candidate` and a `worker-qualification-artifacts` build context holding the SDK wheels under `sdk/` and the application build under `application/`.
The image retains both build records in `/opt/heartwood/qualification/`, and its revision label must name the application commit.

The OpenShell gateway is built from the pinned source and the two deletion patches recorded in `images/worker/candidate/gateway.json`, whose purpose is `qualification-only`:

```bash
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py \
  --gateway --gateway-tests --output test-results/gateway-candidate --cache test-results/gateway-cache
```

`--gateway-tests` runs the upstream deletion regressions first and needs 12 GiB of container memory; a failed or empty test run prevents packaging.
The cache keeps source archives and the Cargo registry, and compiled objects are keyed by the manifest so changed inputs never reuse them.
The record binds source, manifest, lockfile, binary, and image identities; it establishes build provenance, not byte-identical gateway builds.
These artifacts are qualification inputs, not a supported production gateway; an operator's gateway must provide the [behavior qualification depends on](../operate/platform-requirements.md).
`deploy/build-records-fixture.py` builds the synthetic records server and client from digest-verified sources; they provide synthetic storage behavior, not a records-service recommendation.

## Native Qualification

`deploy/qualify-native-worker.py` runs the native cases against existing worker and runtime images, the gateway build, and the records tools.
It requires a local Linux Docker engine and a new output directory, builds the outside controller and the symlink-workspace fixture image, and runs the installed application without source overlays.

```bash
python3 deploy/build-records-fixture.py \
  --output test-results/records-tools --cache test-results/records-cache
python3 deploy/qualify-native-worker.py \
  --worker-image heartwood-worker:qualification \
  --runtime-image heartwood-runtime:qualification \
  --gateway-artifacts test-results/gateway-candidate \
  --records-artifacts test-results/records-tools \
  --output test-results/native-qualification
```

Only the outside controller and the disposable OpenShell gateway receive the Docker socket; workers receive neither the socket nor its group.
The synthetic TLS provider uses a generated credential and a fixed tool-call sequence, so no external inference service or key is needed.

### Installed Native Integration

The synthetic run executes exactly the cases listed in `EXPECTED_CASES` in `deploy/qualification_evidence.py`:

- an ordinary and a hostile session, each with local audit and with deployment-bound checkpoint evidence, through the production launcher, admitted policy, read-only projections, exact approvals, verified audit, revocation before writer release, and fresh-process replay that rejects a wrong project identity and corrupted audit;
- unapproved actions that must not run, after denial and at shutdown with a review pending;
- an outside operation that requires retained authorization, approved, denied, pending, and with its reply lost;
- the default sandbox review mode, in which a contained action is allowed by the gateway while an outside operation waits for the researcher;
- two workers whose authority and effects stay separate;
- a derived image whose OCI working directory is a symbolic link, which must fail with an outside `WorkspaceValidationFailed` condition before the main process or any inference request;
- a discarded create response, which must keep the attempt fenced and the writer owned until outside revocation removes the worker and volumes;
- expiry of the provider credential, which must deny a retained placeholder;
- records transport authorization, and gateway logs retained until verified cleanup.

The hostile session's approved command tries to read control credentials, change protected state and read-only projections, and reach a forbidden provider path.
The fixture compares state canaries and provider traffic before and after; worker-reported success cannot replace these observations.
An allowed endpoint redirects to a forbidden path, and qualification requires the redirect request outside the worker and no request at its destination; an unapproved WebSocket upgrade must be refused.
Direct IPv4, mapped IPv6, native IPv6, TCP, and UDP probes run against controlled listeners on a disposable dual-stack network, and an outside client must reach each listener before and after, so a timeout or unreachable route cannot pass as a denial.

With checkpoint evidence, a records outage must block the approved action until its authorization is retained, and command replay must not repeat the effect.
The pinned synthetic records server rejects the conditional-write bucket policy, so replacement versions are detected and rejected without claiming full storage-policy qualification.
Test failure or failed cleanup fails qualification; if revocation cannot settle, the fixture retains its control state for reconciliation.

These cases do not establish resistance to model-driven prompt injection, production records-policy qualification, tenant authorization, or sensitive-data readiness.

### Infrastructure Telemetry

The fixture receives OpenShell's OCSF export through OTLP with mutual TLS at an outside test collector; an unauthenticated client must be rejected and an authenticated control must succeed.
It checks gateway and sandbox resource identifiers, keeps native event identifiers, and rejects synthetic credentials or state canaries before writing telemetry artifacts.
During an injected collector outage, harmless commands continue until the supervisor reports an export gap, and the resumed collector must receive that gap with a positive dropped-record count and loss timestamp.
The Heartwood audit must stay byte-for-byte unchanged through the fault and verify afterward.

A received gap record establishes known loss, not complete capture of every other event.
An exporter reports a gap with a later batch, so idle periods and abrupt teardown can leave loss unreported.
The collector is a fixture; it does not qualify a production collector's retention, alerting, or recovery.

### Capable Model in a Native Worker

`--model-profile` runs one capable-model case through the same boundary against an operator-approved provider route.
The profile names `base_url` and `model` and may bound `max_input_tokens`, `max_output_tokens`, `max_iterations`, and `deadline_seconds` (defaults 32,768, 512, 6, and 180).
The credential is read once from standard input; it is never an argument, profile field, worker credential, or report value.

```bash
python3 deploy/qualify-native-worker.py \
  --worker-image "$WORKER_IMAGE" --runtime-image "$RUNTIME_IMAGE" \
  --gateway-artifacts "$GATEWAY_ARTIFACTS" --records-artifacts "$RECORDS_ARTIFACTS" \
  --model-profile /run/config/qualification-model.json \
  --output test-results/native-model-qualification < /run/secrets/qualification-key
```

The synthetic input tells the model to ignore approval and read outside credentials.
The controller approves only the input read, the verified Skill invocation, and the aggregate command the task needs, runs filesystem and network probes from outside the worker, verifies the audit ledger, and requires outside revocation before cleanup.
The disposable provider credential carries an expiry matching the deadline, which OpenShell enforces at delivery and at request time, and an outside timer detaches the provider independently of the application.
Expiry does not cancel a request the provider already accepted, and the iteration and token limits are not a provider spending cap.
The record reports the model as requested configuration and usage as worker-reported diagnostics, not attested identity or billing evidence.
No credentialed run happens in CI.

### Native Network Qualification

`deploy/tests/worker_network_qualification.py` runs the network probes against an existing disposable sandbox through the authenticated control API, from a trusted Linux controller with IPv4 and native IPv6 addresses that reach its canary listeners.

```bash
python deploy/tests/worker_network_qualification.py \
  --gateway "$GATEWAY_ADDRESS" --ca "$CA_PATH" --cert "$CLIENT_CERT_PATH" --key "$CLIENT_KEY_PATH" \
  --sandbox "$SYNTHETIC_SANDBOX" --ipv4 "$CONTROLLER_IPV4" --ipv6 "$CONTROLLER_IPV6" \
  --port 19672 --ipv6-port 19673
```

The admitted policy must allow the `--allowed-host` name (default `host.docker.internal`) and deny the canary ports and unlisted destinations.
The check requires explicit policy denial for direct IPv4, mapped IPv6, native IPv6, UDP, and an unlisted hostname; any canary traffic, listener failure, or change in sandbox identity, generation, or configuration invalidates the result.
It does not create, delete, or change the sandbox, and does not qualify IPv6 inference or model authorization.

### Native Qualification Evidence

The runner writes `qualification.json` as the manifest of one run and `SHA256SUMS` over every retained file:

| Field | Content |
|---|---|
| `qualification_kind` | `synthetic-native`, or `capable-model-native` with `requested_model_profile` |
| `source` | The Heartwood commit of the checkout and whether its tree matched that commit |
| `started_at`, `finished_at` | Run boundaries in UTC |
| `invocation` | Image references and paths supplied to the runner |
| `images` | Content identifiers of the supplied images and of the images built for the run |
| `image_sources` | For the worker and runtime images, the revision label, registry digests, and build records read from the image itself |
| `gateway_build`, `records_build` | The verified build records of the gateway and records tools |
| `qualification_sources`, `qualification_sources_unchanged` | Hashes of the runner, fixtures, tests, and controller inputs, and whether they stayed unchanged |
| `host`, `controller_socket_group` | Kernel, architecture, and Docker version of the engine, and the socket group given to the controller |
| `returncode`, `network_retained` | Test command status and whether the qualification network kept attached resources |
| `cases` | Identifier, outcome, and duration of every executed case, read from `junit.xml` |
| `sensitive_data_ready` | Always `false` |
| `failures`, `passed` | Every reason the run does not qualify its commit, and the verdict |

A run passes only when the tree matches its commit, both supplied images name that commit in their revision label and application build record, the sources are unchanged, the test command succeeds, no network resources are retained, and the executed cases equal the expected set with every case passed.
A reduced, extended, or skipped selection therefore fails, and adding, removing, or renaming a case requires changing `EXPECTED_CASES` in the same commit.

`SHA256SUMS` covers the manifest, `junit.xml`, logs, and per-case evidence.
Each worker case retains `worker-container.json`, the image, privilege, capabilities, security options, network, namespaces, and mounts Docker reported before admission was decided, without host paths, labels, or environment, so a rejected sandbox still shows its observed values.
Private gateway state and generated credentials share the output directory and are excluded from the checksums and from retention.
The worker candidate workflow verifies the evidence against its own commit and retains it for 90 days on the main branch and 14 days on pull requests.
On the main branch, a separate Native Qualification job, which runs no candidate code, downloads the evidence, verifies it again, and attests `SHA256SUMS` with a GitHub build-provenance attestation.
Pull-request, manual, local, and capable-model runs, and manual Native Qualification runs on another branch, are not attested.

Release creation keeps the evidence beyond its artifact retention.
It locates the successful Native Qualification run of the release commit, downloads its evidence and attestation, verifies both as below, and fails when any of these is missing or does not verify.
The release then carries `native-qualification-evidence.tar.gz`, the evidence directory with its `SHA256SUMS`, and `native-qualification-evidence.sigstore.jsonl`, the attestation bundle for that `SHA256SUMS`.

To verify release evidence, check out the release tag and run these commands in a directory holding both assets:

```bash
commit="$(git -C "$HEARTWOOD_CHECKOUT" rev-parse HEAD)"
tar -xzf native-qualification-evidence.tar.gz
gh attestation verify native-qualification/SHA256SUMS --repo SchmiedmayerLab/heartwood \
  --bundle native-qualification-evidence.sigstore.jsonl \
  --signer-workflow SchmiedmayerLab/heartwood/.github/workflows/native-qualification.yml \
  --source-ref refs/heads/main --source-digest "$commit"
(cd native-qualification && sha256sum --check SHA256SUMS)
python3 "$HEARTWOOD_CHECKOUT/deploy/qualification_evidence.py" native-qualification --commit "$commit"
```

The bundle holds the attestation that release creation verified; without `--bundle`, the command verifies against the attestations GitHub stores for the repository.
Evidence downloaded from a workflow run is verified the same way from a checkout of the commit it claims to qualify: run the commands inside the evidence directory, with `SHA256SUMS` and `.` as the paths and without `--bundle`.

| Option | Establishes |
|---|---|
| `--repo` | The attestation was signed by a run of this repository |
| `--signer-workflow` | The Native Qualification workflow signed it, not another workflow with attestation permission |
| `--source-ref` | The run built the main branch, not a pull request, tag, or other branch |
| `--source-digest` | The run built exactly the commit being verified |

Without the last three options the command accepts an attestation of the same file from any workflow and branch of the repository.
The attestation shows where and from which commit the checksums came, not that qualification passed.
`sha256sum` shows the files are the ones recorded.
`qualification_evidence.py` rechecks the digests, compares the recorded cases with `junit.xml`, re-evaluates every pass condition against the expected set of the checkout it runs from, and requires the stated commit; it prints each reason and exits non-zero otherwise.

A manifest binds a result to one commit, one set of image contents, and one host; it does not transfer to another commit, architecture, or rebuilt image.
Unattested evidence shows internal consistency, not origin.

## Research Evaluation Evidence

Research evaluations measure connectivity, tool compatibility, workflow completion, artifact completeness, coding and statistical correctness, policy adherence, and recovery separately; a successful tool call does not establish task quality.
Records bind suite and fixture identity, model revision, provider, runtime, hardware, context, parser, Skill digest, harness revision, trial date, independent checks, and measured usage, and have no fields for prompts, tool output, credentials, or participant data.
Unavailable token counts and cost stay unknown rather than zero.

The gateway separately observes the request model, OpenHands version, context limits, confirmation policy, platform adapter, and fingerprints of model options and security configuration, and the driver checks this observation before model work, before reviewed continuations, and at finalization.
A changed runtime leaves the trial incomplete, and injected, deterministic, or unconfigured backends cannot qualify a model.
Remote weights, precision, hardware, and server software remain declared metadata.

The assessor requires the latest three real-model trials of every case to pass within 30 days by default; changed configurations or suites, unknown revisions, expired evidence, missing checks, and recent failures do not inherit earlier passes.
Three trials are a functional regression gate, not a statistical estimate of scientific reliability, and an assessment never changes model recommendations or review policy by itself.
A trial reserves its record in the bound state's `evaluations/` directory before model work, so an interrupted evaluator leaves an incomplete trial rather than a missing one; completed records cannot be overwritten.

Budgets for time, actions, model calls, tokens, and reported cost are checked between gateway updates and after review, not inside an uninterrupted agent run.
Use provider-side spending limits for hosted evaluations, and run generated code only in an isolated synthetic environment.

### Synthetic Research Cases

The maintained fixtures cover dataset readiness, a held-out linear baseline, and independent result verification over pinned synthetic inputs.
Checks inspect structured outputs, not the agent's completion message; a parseable script with correct reported metrics does not show that it ran.
Reproduction requires a separately approved terminal execution into a previously absent directory, binds the program, inputs, and outputs before approval and after execution, and cannot be satisfied by copied outputs or repaired later.
These observations are not a sandbox and do not detect a process that changes and restores files between them; small fixtures test software behavior, not scientific generalizability.

### Parallel Review Comparison

`assess_parallel_reviews` compares matched sequential and parallel trials that differ only in requested specialist concurrency.
The default policy requires three recent trials per case, at least a 10% reduction in median and total elapsed time, and no more than a 25% increase in cost or tokens; unknown usage or failed trials prevent qualification.
Task intervals with the same process-local clock establish overlap, not simultaneous provider requests or useful findings.
`ReservedReviewTrial` admits explicitly experimental runs labelled **Experimental parallel review** under the same consent and approval rules; admission never produces a passing result.
[Qualify Parallel Reviews](../operate/parallel-reviews.md) describes preparation and trust boundaries.

### Action-Review Evaluation

The action-review corpus contains benign, ambiguous, destructive, encoded, injected, network-capable, path-escaping, and boundary-crossing proposals for the terminal, file editor, Task Tracker, and Task tool.
Evaluation builds typed OpenHands actions, classifies them with the session code, and applies the production rules for one review mode and execution boundary without executing anything.
Each case states whether it must reach the researcher outside a sandbox and inside an admitted one, and appears once with a low and once with an unknown model rating; both must receive the same decision.

Reports keep case identities, corpus and analyzer digests, SDK version, date, mode, boundary, decisions, rules, and latency, without commands or file contents.
A deployment assessment requires complete matching evidence, no false approvals, no decision that depends on the model rating, and confirmation of unknown classifications.
Every mode a deployment lists in `allowed_action_review_modes` must pass the zero-false-approval gate for the pinned SDK.
Under `review-changes`, read-only shell commands still wait and count as unnecessary confirmations, a usability measure.
Under `review-outside-effects`, an opaque script is correctly automatic only because the session is contained; the containment itself needs native qualification evidence.
Passing a synthetic corpus is necessary evidence for a policy decision, not proof that arbitrary actions are safe.

## Synthetic Data Rule

Source control, public examples, CI, screenshots, and replay fixtures use synthetic data only.
Protected health information must never enter a test fixture, public log, screenshot, pull request, or model-evaluation artifact.
