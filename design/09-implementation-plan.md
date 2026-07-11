<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 09 — Delivery Roadmap

This roadmap converts the product goals in [01 — Overview](01-overview.md) into ordered delivery gates. It records the test-backed baseline, material readiness gaps, required work, and acceptance criteria. Architecture rationale belongs in [03 — Architecture](03-architecture.md), current platform status belongs in [Platform Support](../docs/platform-support.md), and operational instructions belong in [Documentation](../docs/README.md).

Unchecked items are planned work and are not current capability or support claims. Work starts in priority order unless a security or correctness defect requires immediate attention.

## Non-Negotiable Requirements

- Keep the common researcher path conversation-first and low-configuration across the CLI and web UI, with the notebook bridge exposing the same persisted session.
- Delegate the agent loop, terminal and file tools, action-risk analysis, action confirmation, conversation persistence, provider protocol, and native `SKILL.md` loading to OpenHands and LiteLLM.
- Keep Heartwood focused on biomedical Skill curation, platform and dataset adaptation, route authorization, data-use policy, content-minimized audit records, attestations, and controlled export.
- Support local, air-gapped, and institution-authorized model routes through one non-secret connection catalog that materializes the existing model-profile execution contract.
- Never place model weights, credentials, generated settings, prompt content, response content, or participant-level records in image layers or public artifacts.
- Keep one command and event contract across the CLI, notebook bridge, web UI, scripts, replay, and tests.
- Keep generic AMD64 and ARM64 images aligned and keep platform-derived images as thin extensions of the same Heartwood payload.
- Treat platform network, identity, storage, and access controls as authoritative; describe Heartwood route policy and action-risk analysis as application-layer controls.
- Require synthetic fixtures in source control, public documentation, CI, screenshots, replay traces, and externally shared evidence.
- Do not place compliance evidence packages or reviewer tooling in the primary researcher navigation; keep content-minimized audit export distinct from release and institutional review workflows.

## Current Baseline

### Agent Runtime And Interfaces

- The gateway owns one OpenHands SDK adapter that configures `Conversation`, `Agent`, `LLM`, terminal and file-editor tools, native Skills, persistence, upstream security analyzers, and confirmation policies.
- The CLI, web UI, and notebook bridge use one Heartwood command and event contract for tasks, messages, actions, allow or reject decisions, pause and resume, replay, settings, and audit export. The gateway also owns persisted session creation, listing, title metadata, status derivation, and selection used by the web session rail.
- The web UI is conversation-first and implements persisted session navigation, title editing, typed platform and dataset context, chronological model and tool activity, inline action decisions, a stable composer, responsive session and utility sheets, repository-verification labels for Skills, readable audit activity, local, platform, cloud, and custom model connections, advanced model profiles, and byte-level local-model download progress. Boundary evidence and workflow progress are omitted until typed gateway events provide them rather than being inferred or presented as placeholders.
- **Ask Every Time** maps to OpenHands `AlwaysConfirm`. **Auto-Approve Low Risk** maps to OpenHands `ConfirmRisky` with a `MEDIUM` threshold and unknown actions confirmed. Deployment policy controls which modes may be selected.
- The deterministic backend is limited to unit tests, replay, and no-model integration checks.

### Models, Skills, Policy, And Audit

- A versioned non-secret model-connection and catalog contract discovers local OpenAI-compatible, OpenAI, Anthropic, custom API, and platform-provided research models through one gateway service. Official provider SDKs own cloud listing and pagination, OpenHands and LiteLLM provide compatibility metadata, and the selected exact identifier materializes the existing model-profile execution contract. The CLI and web UI project the same catalog; raw profiles remain an advanced compatibility path.
- Credentials are runtime references to environment variables, mounted files, or managed identity. Provider turns are denied until deployment policy authorizes the declared normalized policy endpoint, capability tier, confirmation mode, and credential reference; platform controls enforce the actual network destination.
- Every configured environment-referenced provider key is blanked in OpenHands terminal subprocesses; only the active key is resolved into the in-process model client.
- Published images contain a CPU llama.cpp runtime and reviewed Hugging Face artifact metadata but no model weights. Downloads are explicit, revision-pinned, size-checked, digest-checked, stored outside image layers, and report byte progress through the gateway. The catalog separates a tool-capable agent demonstration artifact from a coding-output experiment rather than assuming that coding text quality implies OpenHands tool compatibility.
- Repository-verified biomedical Skills load through the OpenHands native loader. Mounted extensions require validation and one recorded installation decision before entering persistent Skill storage.
- Session events capture researcher messages, agent messages, and action summaries required for complete client replay. Exported audit records retain route decisions, action risk, confirmation, tool identity and outcome, Skill identity, and exports while scrubbing prompt, response, action-summary, filesystem-path, row, and secret values.

### Packaging And Verification

- Generic images build natively for `linux/amd64` and `linux/arm64` and publish one multi-platform manifest. Terra publishes a separate AMD64 Docker schema-2 manifest compatible with Leonardo image detection.
- CI verifies no-weight image contents, OpenHands loopback orchestration, both confirmation modes, native Skill loading, fresh named-volume ownership and cross-container recovery, a separately mounted llama.cpp fixture, web and CLI contracts, Jupyter startup, proxy routing, Terra image contracts, and registry media types. An opt-in workflow-dispatch job runs the pinned 7B agent artifact through a network-disabled OpenHands terminal action without making it a pull-request dependency.
- Main publication stages untagged images by digest, validates the exact candidates, creates and verifies immutable commit tags, and moves the generic and Terra channel tags only after every required candidate check passes.
- Python, TypeScript, documentation, licensing, secret scanning, dependency review, CodeQL, container checks, and synthetic replay are repository gates.

## Material Readiness Gaps

1. **Runtime detection is still synthetic.** Default session construction uses `GenericPlatformAdapter` and `LocalFilesystemDataSourceAdapter.synthetic_omop()`. A normal workspace can therefore report the synthetic OMOP fingerprint even when no real dataset adapter has identified workspace data.
2. **Terra is not live-validated.** The image and CI contracts pass, but an immutable published image has not completed the full synthetic workflow, persistence check, and autopause or resume check in Terra's control plane.
3. **The biomedical workflow is not production-validated.** Bundled Skills are deterministic integration implementations and repository-verified, not clinically, statistically, cryptographically, or institutionally approved.
4. **Model capability is unclassified.** Artifact integrity and endpoint connectivity are tested, but no local or hosted model has a benchmark-backed Heartwood capability claim.
5. **The audit log is locally tamper-evident, not authoritative.** Hash chaining detects edits but does not provide deployment-owned signing, retention, or an off-workspace copy.
6. **The project has no stable release channel.** Semantic-version tags, image signing, generated third-party notices, retention automation, support policy, and a published documentation site are not complete.
7. **Session persistence assumes one writer.** The file-backed session and audit stores do not coordinate independent CLI and web processes, so concurrent writers and crash recovery are not release-validated.
8. **Ingress trust is deployment-dependent.** The gateway binds to loopback by default and relies on the platform proxy for authentication, but trusted-proxy configuration, forwarded-prefix handling, WebSocket origin checks, and explicit non-loopback startup policy are not a complete deployment contract.
9. **The web UI has not completed target-user acceptance.** The conversation-first shell, persisted session rail, responsive sheets, inline actions, activity, Skills, and progressive model settings are implemented and automated at desktop and narrow Jupyter viewports. Boundary evidence and workflow progress still lack typed events, denied and degraded states need broader accessibility coverage, and no representative-researcher or platform-administrator walkthrough has been completed.
10. **Tool credential isolation is deployment-dependent.** Configured environment-referenced provider keys are masked from terminal subprocesses, but a mounted credential file or managed identity available to the interactive workspace user is not isolated from agent-executed code by the current in-process architecture.
11. **First-run and persistence setup is not unified.** The generic runtime uses a state volume and a model-cache volume, path overrides remain distributed, and no shared setup flow verifies durable storage or migrates prior state. [Issue #22](https://github.com/SchmiedmayerLab/heartwood/issues/22) owns this work.

## Priority 1 — Release-Candidate Runtime Contract

**Objective:** make runtime identity, persistence ownership, ingress trust, and image publication deterministic before asking a live platform to validate the artifact.

### Deliverables

- [ ] Replace the implicit synthetic data-source default with an explicit unconfigured data source. Enable the synthetic OMOP adapter only through a named fixture or demonstration configuration.
- [ ] Add a minimal Terra platform adapter selected from detector evidence. It must expose platform identity, persistent paths, proxy assumptions, and a conservative default policy without implementing a parallel Terra client.
- [ ] Make the gateway the sole writer for an active session. Route CLI operations through the running gateway or enforce an interprocess session lock, and add concurrent-command, duplicate-writer, interrupted-append, and recovery tests.
- [ ] Implement the canonical versioned state root, one-volume default, optional split model cache, migration, restart checks, and shared first-run setup defined in [Issue #22](https://github.com/SchmiedmayerLab/heartwood/issues/22).
- [ ] Define and enforce the ingress trust contract: loopback by default, explicit trusted-proxy mode for platform deployment, validated base-path and forwarded-prefix handling, WebSocket origin checks, and refusal of accidental unauthenticated non-loopback exposure.
- [ ] Define and validate the model-only credential isolation contract. Keep provider environment values out of tool subprocesses, require least-privilege identities for analysis, and use a supported OpenHands remote workspace or platform-native process boundary whenever mounted model credentials or identity tokens must be inaccessible to coding tools.
- [ ] Publish immutable generic and Terra image tags from one commit and record the image, base-image, and application dependency digests.
- [ ] Evaluate `terra-jupyter-python:1.1.6` against Terra's current supported base options. Retain the current base unless a replacement passes the complete user, kernel, entrypoint, Leonardo route, proxy, storage, model-runtime, and publication contract.

### Exit Criteria

- An unconfigured runtime reports no dataset rather than a synthetic OMOP match.
- Terra detection selects the Terra adapter and a conservative policy without adding a Terra client or credential store.
- Independent writers cannot corrupt or fork a session, and interrupted writes recover deterministically.
- The CLI and web UI create, list, and resume the same gateway-owned sessions without raw-path access or browser-owned session state.
- Generic and Terra deployments recover the same non-secret configuration and session state after restart through one documented persistence contract; secrets remain external runtime references.
- The gateway cannot be exposed beyond loopback without explicit trusted-proxy configuration and origin validation.
- Model-only credentials are not readable by terminal or file tools in the declared release deployment; identities intentionally shared with analysis code have documented least privilege and platform evidence.
- Immutable generic and Terra candidate images from one commit pass the complete CI and registry contract.

## Priority 2 — Researcher Web Experience

**Objective:** turn the implemented web interface into a simple, evidence-backed coding-agent experience without adding browser-owned agent, policy, provider, or workflow behavior.

### Deliverables

#### Pass 1 — Typed Evidence Completion

- [ ] Add typed boundary, credential-reference, active Skill, and workflow-progress events at the adapters that own that evidence. Project those records in the header, transcript, and activity view. Represent absent evidence as unknown or unconfigured, and make no model capability claim without benchmark evidence.
- [ ] Add gateway-supplied contextual task starters and workflow progress. Keep the application shell workflow-neutral; expose platform- or Skill-specific steps only when OpenHands or a typed Skill event supplies them.
- [ ] Add negative view-model and component tests proving that malformed, absent, or denied evidence cannot produce stronger boundary, route, Skill, export, or capability labels.

#### Pass 2 — Interface Assurance

- [ ] Cover unconfigured, denied, degraded, and reconnecting model routes; streamed reconnects; session restart recovery; and failed Skill, download, and audit operations across direct and Jupyter-proxy paths.
- [ ] Complete keyboard traversal, focus-order, reduced-motion, tablet reflow, status-announcement, and screen-reader checks for the conversation, composer, inline decisions, session sheet, and utility sheets. Keep every automated fixture synthetic.

#### Pass 3 — Researcher Acceptance

- [ ] Run the synthetic reference-task walkthrough with representative biomedical researchers and a platform administrator. Record only content-free usability findings and resolve or explicitly defer every release-blocking comprehension, navigation, configuration, or recovery defect.

### Exit Criteria

- A researcher can create or resume a session, configure or select an authorized model route, submit a task, follow model and tool activity, allow or reject an action, pause or resume, and export the audit record without reading operator documentation.
- OpenAI, Anthropic, custom OpenAI-compatible, local-runtime, and platform-provided research connections expose the exact models available through their upstream source without a Heartwood cloud-model table; the web UI and CLI project the same normalized catalog.
- The web UI and CLI act on the same persisted session and produce equivalent command and event outcomes through the gateway, including through the Jupyter proxy.
- Every boundary, route, Skill, risk, tool, and export label is traceable to typed gateway data; unknown state remains visible and no vendor, compliance, or model-capability claim is hard-coded.
- Automated desktop, mobile, keyboard, accessibility, restart, and Jupyter-proxy tests pass, and the formative walkthrough has no unresolved release-blocking usability finding.
- The browser owns presentation state only and contains no parallel agent loop, conversation store, policy decision, risk classifier, provider adapter, Skill activation protocol, or fixed biomedical workflow.

## Priority 3 — Live Terra Acceptance

**Objective:** prove that the immutable release candidate works as a coherent researcher experience in Terra's real control plane without repository access.

### Deliverables

- [ ] Run the published Terra image in a synthetic workspace and verify the notebook route, Heartwood kernel, authenticated proxy path, CLI, web UI, notebook bridge, and one shared persisted session.
- [ ] Configure one genuine model route for the live demonstration. The preferred local path stores reviewed weights on the persistent disk and starts the packaged CPU runtime; an institution-authorized in-perimeter endpoint may be used when local hardware is insufficient. Record the route type and policy decision without credentials or prompt content.
- [ ] Complete one model response and one terminal or file action through OpenHands. Verify allow and reject through both primary interfaces and verify **Auto-Approve Low Risk** only when the synthetic Terra policy explicitly permits it.
- [ ] Restart the application and exercise Terra autopause and resume. Confirm that model settings, action settings, installed Skills, OpenHands conversation state, Heartwood events, and scrubbed audit records remain consistent.
- [ ] Export a synthetic evidence bundle containing immutable digests, route and credential-reference metadata, action decisions, replay counts, proxy behavior, persistence results, and limitations.
- [ ] Publish a concise release-candidate tutorial that starts from the GHCR image reference and covers both a local endpoint and a deployment-authorized provider route.

### Exit Criteria

- The immutable Terra image completes the documented synthetic workflow in a real workspace from a clean persistent disk.
- Jupyter startup, the web proxy, CLI and web parity, one genuine model response, one real tool action, restart persistence, and autopause or resume are evidenced without controlled data or secrets.
- The platform support matrix marks the tested artifact live-validated while retaining institution-approval and controlled-data limitations.
- A second maintainer can reproduce the workflow from published documentation without a repository checkout or undocumented setup step.

## Priority 4 — Terra And OMOP Reference Workflow

**Objective:** replace integration fixtures with a real, read-only Terra and BigQuery OMOP workflow that produces policy-checked aggregate outputs and reviewable evidence.

### Deliverables

- [ ] Extend the Terra adapter with deployment-owned identity and credential binding, private or approved model endpoints, persistent workspace paths, and versioned policy loading.
- [ ] Implement a read-only BigQuery OMOP data-source adapter using platform-provided identity and maintained Google client libraries. Do not implement a custom query transport or credential store.
- [ ] Detect OMOP from deterministic schema evidence, report confidence and evidence, and return no match when required tables or permissions are absent.
- [ ] Implement dataset-aware Skill ranking over the existing repository-verified bundle. Show the proposal and evidence, support researcher correction, and record the final selection without introducing per-activation approval prompts.
- [ ] Complete the reference workflow: cohort definition, data-quality checks, baseline model, aggregate results, count-floor enforcement, egress attestation, and explicit reviewed export.
- [ ] Define typed result and failure contracts for each Skill and add deterministic synthetic tests, malformed-data cases, permission failures, empty cohorts, count-floor boundaries, and replay fixtures.
- [ ] Validate each reference Skill through independent security and clinical or statistical review before assigning controlled-data readiness.
- [ ] Run controlled-data validation only in an approved private environment. Store no participant-level values, prompts, responses, screenshots, logs, or traces in source control or public CI.
- [ ] Verify that platform egress controls, model-route policy, credential references, action confirmation, audit scrubbing, and export authorization remain distinct and produce the required reviewer evidence.

### Exit Criteria

- The runtime reports real Terra and OMOP evidence rather than the synthetic fixture and fails closed when identity, schema, permissions, or policy are incomplete.
- The complete reference workflow is reproducible on synthetic OMOP data in CI and separately validated under the approved controlled-data protocol.
- Every exported artifact is aggregate, policy-checked, attested, and independently reviewable.
- Repository-verified and controlled-data-ready Skill status are visibly distinct.

## Priority 5 — Assurance And Stable Release Governance

**Objective:** establish benchmark, security, supply-chain, documentation, and governance evidence sufficient for a supported stable release.

### Deliverables

- [ ] Define a pinned synthetic OMOP benchmark with coding correctness, biomedical task completion, tool-use reliability, policy adherence, and failure-analysis outputs.
- [ ] Benchmark selected local and in-perimeter models before assigning autonomous, supervised, or experimental capability tiers. Record model, quantization, runtime, hardware, prompts, harness, and failure cases.
- [ ] Evaluate the pinned OpenHands analyzer ensemble against representative benign, ambiguous, destructive, encoded, prompt-injected, and network-capable research actions. Define deployment acceptance thresholds before permitting `confirm-risky` in managed policies.
- [ ] Add an OpenHands dependency-upgrade gate that exercises native event translation, confirmation, persistence and resume, Skill loading, tool execution, and offline container integration against the resolved SDK and tools versions.
- [ ] Add deployment-owned audit signing or checkpointing, retention guidance, and an authoritative export path where institutional policy requires them.
- [ ] Add versioned migrations and backward-compatibility tests for model settings, action settings, session events, audit records, Skill metadata, and persisted OpenHands conversations before promising a stable support window.
- [ ] Replace full-log verification on every append with a locked incremental append and explicit full-verification operation while preserving deterministic tamper detection and crash recovery.
- [ ] Implement immutable-source and digest verification for external Skills, real signature verification, review provenance, release channels, and installation policy before enabling remote Skill acquisition.
- [ ] Publish semantic-version image tags, release notes, compatibility policy, software bill of materials, provenance, cryptographic signatures, generated third-party notices, and a support window.
- [ ] Implement graph-aware GHCR retention automation in report-only mode first. Starting from moving release tags, retained immutable tags, and attestation referrers, traverse the complete manifest reachability graph; report only untagged and unreferenced versions older than the retention threshold before enabling narrowly scoped deletion.
- [ ] Publish the README, operational tutorials, design set, API reference, image reference, security model, status matrix, and limitations as a versioned static documentation site with build, internal-link, external-link, accessibility, and tutorial-command checks.
- [ ] Document maintainer roles, security response, Skill review ownership, release authority, deprecation policy, and succession.

### Exit Criteria

- Every capability and support claim links to a reproducible benchmark, automated test, live-platform evidence, or documented institutional decision.
- Stable artifacts are immutable, signed, attributable, documented, and recoverable under the retention policy.
- Dependency and Skill updates have focused conformance gates and do not require changes across multiple interface implementations.
- The release documentation distinguishes implemented, CI-validated, live-validated, release-ready, and institution-approved status.

## Priority 6 — Conditional Expansion

**Objective:** add breadth only after the Terra and OMOP path is stable, measured, and supportable.

### Deliverables

- [ ] Select one second biomedical data type based on a documented researcher need, available synthetic fixtures, domain-review ownership, and a maintainable platform access path.
- [ ] Select one second platform using explicit criteria: user demand, supported base image, identity and proxy model, registry compatibility, persistent storage, policy ownership, live test access, and maintainer capacity.
- [ ] Implement the second platform through the existing adapter SPI, declarative platform manifest, shared Dockerfile, common payload, registry verifier, and platform smoke harness.
- [ ] Add GPU runtime guidance and tests only for a concrete maintained vLLM, SGLang, Ollama, or llama.cpp deployment with supported hardware. Keep GPU inference outside the baseline CPU image when the platform runtime requires vendor-specific libraries.
- [ ] Evaluate an OpenHands remote workspace or platform-native sandbox before supporting deployment-owned unattended execution. Do not expose `NeverConfirm` through researcher settings without isolation, recovery, and audit evidence.
- [ ] Add batch workflow export through established CWL, WDL, or Nextflow engines only after the interactive reference workflow has stable typed inputs and outputs.
- [ ] Add consented, scrubbed trajectory export and out-of-boundary replay ingestion only after privacy review, purpose limitation, deletion policy, and automated content checks are defined.
- [ ] Add multi-user or remote-agent-server deployment only when a supported platform requires it and supplies identity, tenant isolation, persistence, and operations ownership.

### Exit Criteria

- Each added platform or data type has the same adapter, policy, image, integration, live-validation, documentation, and ownership evidence as the reference path.
- Expansion does not add another agent loop, provider client, tool protocol, Skill format, UI state model, registry service, workflow engine, or orchestration layer.

## Start Conditions For Deferred Capabilities

| Capability | Required Before Work Starts |
|---|---|
| Second platform | Stable Terra and OMOP reference workflow plus a platform-adapter conformance suite. |
| Second biomedical data type | Stable OMOP workflow, synthetic fixtures, and named domain reviewers. |
| Remote or unattended execution | Supported OpenHands remote workspace or platform sandbox, recovery semantics, and deployment-owned audit evidence. |
| `NeverConfirm` | Unattended-execution approval plus isolation and adversarial validation; it remains unavailable to researcher settings. |
| GPU image or profile | Named runtime, hardware target, maintainer, benchmark, and native CI or scheduled validation path. |
| Batch workflow export | Stable typed inputs and outputs from the interactive reference workflow and an established target engine. |
| Remote Skill distribution | Immutable source resolution, digest and signature verification, review provenance, revocation, and release ownership. |
| Multi-user service | Platform requirement, identity integration, tenant isolation, persistence, operations, and incident-response ownership. |

## Cross-Cutting Acceptance Rules

- Tests scale with risk: pure policy and schema paths require deterministic branch coverage; interfaces require contract and accessibility tests; platform and runtime paths require integration and live evidence.
- All public artifacts remain synthetic. Controlled validation produces only approved, content-minimized evidence outside the public repository.
- Security and compliance claims identify the enforcing platform control, Heartwood application control, test evidence, and residual limitation.
- New provider, runtime, platform, or Skill behavior uses a maintained upstream library or standard behind a narrow adapter.
- A roadmap item is complete only when its implementation, tests, operational documentation, status matrix, and acceptance evidence agree.

## Repository And Ownership Strategy

- Keep the application, adapters, schemas, repository-verified Skills, images, tests, and documentation in one repository through the Terra and OMOP reference workflow.
- Keep generic and platform-derived images in one build graph so every platform variant consumes the same Heartwood payload and dependency lock.
- Keep repository-verified Skills checked in until external distribution has independent ownership, immutable releases, signature verification, revocation, and compatibility testing.
- Split a component only when it has an independent release cadence, named maintainers, a stable versioned contract, and a demonstrated need that cannot be met by the current workspace.
- Keep design documents canonical for product and architecture. Use GitHub Issues and Projects for assignment and delivery status, with links back to the relevant roadmap acceptance gate. Create a focused issue when a roadmap deliverable becomes active and record its owner, dependencies, acceptance evidence, and target release without duplicating design rationale; [Issue #22](https://github.com/SchmiedmayerLab/heartwood/issues/22) is the first-run and persistence work item.
