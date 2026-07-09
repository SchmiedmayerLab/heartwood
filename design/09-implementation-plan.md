<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 09 — Implementation plan

This document is the implementation checklist. It records the current repository baseline, the remaining Phase 0 passes, and the later phases. Architecture rationale belongs in [03](03-architecture.md), platform assumptions belong in [02](02-platforms.md), and development-tooling policy belongs in [08](08-development.md).

## Current Baseline

The repository is at **0G in progress**. Passes 0A through 0F are implemented and must stay green while the local-runtime, agent-server, image, CI, and documentation work lands. The first 0G slice adds the local-runtime profile contract, multi-architecture generic-image publishing and smoke coverage, and keeps the current deterministic stub path clearly labeled as a fixture, not a real inference runtime.

### Implemented In 0A — Repository Bootstrap And CI Baseline

- Repository health files, license files, contributor metadata, workspace configuration, and package layout exist.
- The Python workspace is managed with `uv`, strict linting, type checking, pytest, coverage, and REUSE compliance.
- CI-facing checks are mirrored locally through repository tooling.
- Synthetic-only fixture policy and no-live-data checks are part of the repository baseline.

### Implemented In 0B — Contracts, Schemas, And Fixtures

- Adapter protocols exist for platform, model provider, data source, and registry boundaries.
- Versioned schemas exist for policy profiles, model-call decisions, egress attestations, audit events, detector evidence, approvals, confirmation requests, and `heartwood.*` skill metadata.
- The session command contract includes `detect`, `approve`, `deny`, `chat`, `run`, `pause`, `resume`, `replay`, and `audit.export`.
- The session event contract includes command receipt, detection proposal, approval recording, policy decision, model-call decision, agent message, tool-call proposal, confirmation request, confirmation resolution, tool execution, pause, resume, audit export, and errors.
- Synthetic fixtures cover environment probes, OMOP-like tables, denied egress, egress attestation, policies, detector evidence, skill metadata, approvals, confirmation requests, and expected audit export records.
- Schema tests validate the checked-in synthetic fixtures and reject malformed records.
- Adapter conformance tests validate deterministic fake implementations.

### Implemented In 0C — Deterministic Core Harness

- The core adapter exposes an SDK-neutral event-streaming backend facade.
- The deterministic backend emits assistant-message, tool-call-proposal, confirmation, and tool-execution backend events without a live model or network.
- The session service accepts commands, persists commands/events, emits structured session events, and records hash-chained audit events.
- The session service handles detection, approval, chat, run, pause, resume, replay reads, and audit export.
- The service translates backend events into the shared session event contract.
- Tool-call confirmation requests and confirmation resolutions are represented in the event stream.
- Model-call policy decisions remain deterministic and offline.
- Model-call approval is required before deterministic tool execution is marked approved.
- Generic platform, local filesystem data, and fake/local model-provider adapters are available for tests and synthetic replay.
- Dataset fingerprints use filenames and headers, not row values.
- Audit export scrubs prompt, response, row, record, result, value, secret, token, and message-content fields.
- The CLI detection command runs through the session service and persists local state without network access.

### Implemented In 0D — Prototype Skills And Replay

- Local `SKILL.md` verification checks metadata schema, trust tier, signature placeholder, network requirement, declared tools, and approval copy.
- `skills/bundle.toml` resolves checked-in verified skills through the local verifier.
- External skill import specifications are validated for pinned refs, resolved commits, hashes, metadata, and provenance before import.
- Three verified prototype skills exist: OMOP cohort summary, aggregate export, and baseline model.
- Prototype skill tests cover scripts, metadata, declared tools, deterministic outputs, and aggregate export guards.
- The full synthetic replay fixture includes expected tool calls, policy decisions, streamed session/audit events, aggregate outputs, and attestations.
- Replay validation includes the 0C event-stream additions: agent message, tool-call proposal, and confirmation resolution.

### Implemented In 0E — Gateway And Agent-Server Binding

- `packages/gateway` exists and is registered in the Python workspace.
- ASGI HTTP command handling accepts the existing session command contract and returns session events.
- Replayable ASGI WebSocket streams expose the existing session event contract and support reconnect replay.
- Gateway command handling routes through the session service and preserves disk-backed session events and audit records.
- Pause, resume, confirmation request, confirmation resolution, policy denial, and malformed command paths are covered through gateway contract tests.
- The managed agent-server boundary owns a configurable child process, rejects non-local bindings, requires the Local runtime, and blocks direct client endpoint exposure.
- The agent-server binding can be disabled for offline CI and synthetic replay.
- OpenHands-style message, tool-call, confirmation, and tool-result events are translated behind the core-adapter backend facade.
- The default gateway path remains deterministic and in-process for offline commands and tests.
- The model egress proxy evaluates policy before invoking the downstream model path and records attestation data.
- Tests cover HTTP command handling, WebSocket streaming and replay, gateway lifecycle, fake agent-server translation through the session contract, localhost-only binding, policy denial, denied egress invocation, and invalid requests.

### Implemented In 0F — CLI, Notebook Bridge, Image, And Reviewer Packet

- The CLI routes `detect`, `chat`, `run`, `approve`, `deny`, `pause`, `resume`, `replay`, `audit export`, and `reviewer packet` through the session gateway and shared session command/event contract.
- CLI transcript rendering exposes detection proposals, policy decisions, agent messages, tool proposals, confirmation requests/resolutions, tool execution, lifecycle events, and audit export events.
- CLI tests cover detection, chat, run transcripts, replay, audit export, reviewer packet generation, and scrubbed command persistence.
- `packages/notebook` exists and is registered in the Python workspace.
- The notebook API exposes the same gateway-backed operations as the CLI and projects session events into typed notebook view models for chat, activity, dataset proposals, skill/tool proposals, approval controls, policy status, export actions, and paused state.
- The notebook widget bridge renders deterministic widget specifications without a notebook runtime and optional `ipywidgets` sections when the runtime is available.
- Notebook tests derive view models from the same session event stream used by the CLI.
- `packages/compliance` exists and is registered in the Python workspace.
- The reviewer packet generator validates the synthetic policy profile and egress attestation through shared schemas, exports scrubbed audit JSONL, and writes the reviewer index, data-flow diagram, fixture statement, dependency/license summary, and limitations.
- Reviewer packet tests use checked-in synthetic fixtures and scrubbed session audit records only.
- `images/generic` contains the generic Linux/Jupyter-capable image definition and Docker Compose smoke-test configuration.
- The generic image packages the Python workspace, CLI, gateway, notebook bridge, synthetic fixtures, verified skills, runtime dependencies, and a deterministic loopback model stub.
- The generic image does not contain an LLM inference runtime or model weights; 0F proves the policy-gated local endpoint integration path, not model inference quality or local model performance.
- The generic image does not pin or launch a production OpenHands agent-server; current OpenHands coverage is the gateway-owned localhost process boundary and fake event translation through the backend facade.
- The CLI supports an explicit `run --local-model` mode that invokes only allowlisted HTTP loopback endpoints, records safe response metadata, and keeps prompt/response content out of persisted session events and audit exports.
- Docker Compose disables runtime network access for the smoke service and runs the offline stack script end to end.
- The offline stack smoke script starts the loopback model stub, runs detection, approves the synthetic model call, runs the agentic CLI through the local model endpoint, exports the scrubbed audit log, and generates the reviewer packet.
- The container image publish workflow builds `images/generic/Dockerfile` on `main` for `linux/amd64` and `linux/arm64` and publishes `dev-main`, `main`, and commit-SHA tags to GitHub Container Registry.
- `docs/getting-started-offline.md` documents pull-and-run usage from the published image and the local Compose workflow.
- The container smoke workflow runs the offline stack smoke on `linux/amd64` and `linux/arm64` for pull requests, pushes to `main`, and manual dispatch.
- Static image tests verify the generic image includes the expected runtime surfaces, declares the local-runtime profiles, separates CPU and GPU runtime profiles, disables runtime network access through Compose, runs the loopback stub profile, publishes the expected image tags, and covers both baseline Linux architectures.

### Current Exclusions

- No TypeScript web UI exists yet.
- No Server-Sent Events fallback exists yet.
- No `jupyter-server-proxy` route validation exists yet.
- No static documentation site is published yet; tutorial documentation is checked in as Markdown.
- No real-platform adapter or controlled-data validation exists yet.
- No production LiteLLM provider integration is wired; the gateway exposes the policy-gated invocation point.
- No production OpenHands package/client command is pinned in an image or launched by container CI; the gateway exposes the managed localhost-only process boundary and backend facade.
- No LLM inference runtime, model-serving process, or model weights are bundled; the generic image uses a deterministic loopback stub to prove the integration path.
- A checked local-runtime profile contract exists for `stub-loopback` and `llama-cpp-cpu`, but the real inference dependency, model artifact, checksum verification, and load/query smoke test remain unimplemented.

## Phase 0 Requirements

- Use synthetic fixtures only for development, replay, CI, examples, screenshots, audit exports, and reviewer packets.
- Keep controlled data, participant-level rows, prompt content, response content, secrets, live identifiers, and non-synthetic source markers out of checked-in fixtures, replay traces, tests, logs, and exported artifacts.
- Keep platform-specific behavior behind typed adapters and conformance tests.
- Keep the session command/event contract as the only execution contract exposed to the CLI, notebook bridge, researcher web UI, scripts, and CI.
- Require explicit human approval before skill activation, model egress, tool-call execution, or non-verified skill use.
- Deny network egress by default; allow only configured in-perimeter model endpoints through the policy layer.
- Persist resumable session state and hash-chained audit logs on workspace disk.
- Export only scrubbed JSONL audit artifacts and attestations.
- Run repository validation, Python quality checks, type checks, unit tests, replay tests, fixture-lint checks, coverage gates, and REUSE checks before merge.

## Remaining Phase 0 Passes

### 0G — End-to-End Local Runtime And Published Documentation

**Fit and sequencing**

- Land this pass before the web UI because it proves the execution substrate the web UI will drive: local inference, the managed OpenHands agent-server, the gateway, the CLI, policy, audit, reviewer packet, image packaging, and CI.
- Keep this pass integration-focused; do not add the researcher dashboard or platform proxy UI in 0G.
- Keep synthetic fixtures only; the local model test proves offline load, query, policy gating, event flow, and scrubbing, not biomedical quality.

**Landed in the first 0G slice**

- `images/generic/local-runtime/profiles.toml` declares `stub-loopback` as the implemented deterministic fixture and `llama-cpp-cpu` as the selected real local inference profile.
- The `llama-cpp-cpu` contract defines `linux/amd64` and `linux/arm64` support, CPU/GPU expectations, memory floor, localhost serving API, startup and shutdown behavior, failure modes, GGUF artifact policy, checksum policy, license posture, cache location, build-time resolution, runtime resolution, and CI expectations.
- `llama-cpp-cuda` is recorded as a deferred optional NVIDIA acceleration profile, separate from the portable CPU baseline and requiring explicit host GPU runtime support.
- The offline smoke entrypoint starts local model access through a profile-aware launcher while keeping the default PR/Compose path on `stub-loopback`.
- Static tests verify the runtime profile contract, launcher routing, Docker network-off smoke command, multi-architecture smoke matrix, and published image tags.

**Remaining required work**

1. Convert `run --local-model` from a loopback-stub smoke hook into the real local-runtime path while preserving the deterministic stub profile for fast tests and PR smoke checks.
2. Require explicit model-call approval before any local model invocation, not only before the following tool execution.
3. Add the pinned `llama-cpp-cpu` runtime dependency, a tiny same-runtime CI model artifact, hash verification, provenance metadata, and an offline load/query smoke test that runs without public runtime network access on at least `linux/amd64`, then add `linux/arm64` once the pinned wheel and artifact path are stable.
4. Define the release model artifact strategy: source, license posture, redistribution allowance, checksums, provenance record, size limits, cache location, build-time versus runtime resolution, and the local tutorial model choice.
5. Pin the production OpenHands SDK and agent-server command behind the gateway facade after license and replay checks pass.
6. Start the OpenHands agent-server as a gateway-owned localhost-only child process in the generic image and keep clients from receiving its direct endpoint.
7. Route all model access through the gateway policy layer; the agent-server must not hold a separate public model egress path.
8. Add an offline local-stack entrypoint that starts the selected local runtime, starts the gateway-managed agent-server, runs the agentic CLI turn, exports the audit log, and generates the synthetic evidence bundle.
9. Extend the generic image to include the selected local runtime profile, pinned runtime dependencies, verified skills, OpenHands runtime dependency, and all scripts needed to run without a repository checkout.
10. Publish the generic image to GitHub Container Registry on `main` with stable development tags and commit-SHA tags.
11. Update the checked-in Docker quick start and offline tutorial to describe the selected local-runtime path, resource requirements, generated artifacts, and limitations.
12. Keep optional GPU acceleration behind a separate runtime profile, Docker device configuration, and self-hosted GPU or scheduled platform checks; do not make it a requirement for the generic CPU image.
13. Publish a self-contained GitHub Pages documentation site from checked-in project material, including design documents, goals, current limitations, and image usage instructions.
14. Link-check the static documentation site and keep it free of external CDN dependencies.

**Required tests**

- Unit tests for approval-before-invocation, policy denial, loopback-only enforcement, local-runtime failure handling, response metadata extraction, and prompt/response scrubbing.
- Contract tests for CLI, gateway, OpenHands backend translation, audit export, and reviewer packet generation over the same session command/event stream.
- Image tests that verify the local runtime profile, OpenHands command, verified skills, tutorial scripts, and offline smoke entrypoint are present.
- Docker Compose smoke tests that disable runtime network access and run the local-stack entrypoint end to end on `linux/amd64` and `linux/arm64` where the profile claims support.
- CI must run a small local inference artifact through the selected runtime on pull requests; if the release model is too large for PR CI, use a tiny same-runtime artifact in PR CI and validate the larger local tutorial profile on `main` or scheduled release checks.
- GPU acceleration tests require a separate CUDA profile and a GPU-capable runner; standard GitHub-hosted pull-request CI is not a GPU gate.
- CI must verify that the offline smoke can run from the built image and that no repository checkout is required for the documented Docker path.
- CI must build the static documentation site, run Markdown/link checks, and publish GitHub Pages only from `main`.
- Audit and reviewer-packet tests must prove prompt text, response text, participant-level data, secrets, and non-synthetic markers are absent from persisted artifacts.

**Exit gates**

- A documented Docker-only command can pull the generic image and run the offline local-stack smoke test without public runtime network access.
- The smoke test starts a real local inference runtime, runs a model call after explicit approval, starts the managed OpenHands agent-server behind the gateway, completes one synthetic agentic CLI turn, exports a scrubbed audit log, and generates the synthetic evidence bundle.
- The same session command/event contract drives the CLI, notebook bridge, gateway, and OpenHands backend path.
- Prompt and response content are not persisted in commands, session events, audit exports, reviewer packets, or CI logs.
- GitHub Actions covers the image smoke, local inference runtime profile, OpenHands behind-gateway path, docs build, docs link checks, and GHCR publishing path.
- The static documentation site is available from GitHub Pages and matches the checked-in README, tutorial, design documents, goals, and limitations.
- The generic image publishes a multi-architecture manifest for `linux/amd64` and `linux/arm64`; any architecture excluded from a runtime profile is explicitly documented in the profile.

### 0H — Researcher Web UI And Platform Surfacing

**Fit and sequencing**

- Start this pass after 0G so the web UI is only an additional presentation adapter over a proven local-runtime and agent-server stack.
- Do not introduce a second session contract, separate agent-server path, or browser-only policy implementation.

**Required work**

- Add `packages/webui`.
- Build the web UI as a standalone TypeScript single-page app on `spezi-web-design-system` and `spezi-web-configurations`, using Vite and Vitest, bootstrapped from `spezi-web-template-application`.
- Render chat, dataset cards, proposed skills, approval prompts, policy status, activity trace, count-floored export, and attestation from gateway events.
- Use WebSocket as the primary transport.
- Add Server-Sent Events fallback.
- Rehydrate after reconnect by replaying the session event log.
- Build for sub-path serving with relative asset, API, and WebSocket bases.
- Ship self-contained assets with no external CDN.
- Surface the generic image UI through `jupyter-server-proxy`.
- Validate Terra and Seven Bridges proxy paths through their Jupyter/Data Studio routes.
- Validate DNAnexus first through `jupyter-server-proxy`; keep `httpsApp` as the platform-native upgrade path.
- Inherit identity from the platform proxy; do not add a Heartwood-owned login.
- Keep the agent-server localhost-only behind the gateway and session key.
- Add npm-side license compatibility checks.

**Required tests**

- ESLint, Prettier, strict `tsc`, Vitest, and coverage.
- Web UI view-model snapshot tests generated from synthetic session events.
- Component tests for event rendering, approval decisions, policy denial, reconnect replay, count-floor export, and attestation display.
- Proxy smoke tests for `jupyter-server-proxy` sub-path serving.
- Runtime asset tests proving no external CDN or public network dependency.

**Exit gates**

- The full local-runtime synthetic workflow from 0G runs from the web UI on the generic image.
- Web UI, notebook bridge, and CLI observe the same session event semantics.
- Web assets require no public network access at runtime.
- Proxy smoke tests pass.

## Phase 1 — First Reference Platform And Controlled-data Validation

**Required work**

- Select one primary pilot platform and one secondary adapter target.
- Implement the primary platform adapter.
- Wire the primary platform model endpoint, credential allowlist, image base, policy profile, and platform note.
- Build an air-gapped image variant with vendored dependencies and verified skills.
- Verify skill signatures at build time and load time.
- Expand the reviewer packet with platform-specific controls and language.
- Validate the Phase 0 workflow on controlled data only after synthetic replay, reviewer packet, and primary-platform policy reviews pass.

**Exit gates**

- Primary-platform adapter conformance tests pass.
- Platform policy profile blocks non-compliant egress and exports.
- Controlled-data validation produces only approved aggregate artifacts.

## Phase 2 — Skill Breadth And Second Data Type

**Required work**

- Add the community skill tier.
- Add signing flow, approval UX, scheduled scans, and skill eval gates.
- Aggregate external skill registries through verified import.
- Add one second data adapter, either genomics/VCF or FHIR.
- Add one second compliant in-boundary model path.

**Exit gates**

- Community skills cannot run without explicit approval.
- Verified import rejects unpinned, unsigned, or metadata-incomplete skills.
- The second data adapter passes conformance and synthetic replay tests.
- The second model path passes policy and attestation tests.

## Phase 3 — Scale-out And Improvement Loop

**Required work**

- Emit Dockstore-registered CWL, WDL, or Nextflow workflows for batch scale.
- Operationalize the export, validate, improve loop across consenting sites.
- Add local-model support as an explicit supervised tier.
- Add additional platform adapters based on demand and maintenance capacity.

**Exit gates**

- Emitted workflows are reproducible from audited session state.
- Improvement artifacts contain no participant-level data.
- Local-model mode is capped by capability tier and supervised controls.

## Cross-cutting Test Requirements

- Adapter contract tests must cover every platform, model, data, and registry adapter.
- Schema compatibility tests must cover versioned policy, audit, skill metadata, confirmation, approval, session, and attestation records.
- Replay tests must compare expected session/audit events, tool calls, policy decisions, outputs, and attestations against golden fixtures.
- Interface contract tests must derive CLI transcripts, notebook view models, and web-UI view models from the same synthetic session events.
- Network-deny tests must cover blocked public egress, allowed in-perimeter endpoints, malformed endpoints, and denied runtime dependency installation.
- Skill tests must cover metadata validation, declared tools, approval copy, script execution, export guards, and replay or golden output.
- Fixture lint must block secrets, direct identifiers, PHI-shaped values, and non-synthetic source markers before merge.
- Audit tests must cover hash-chain verification, tamper detection, scrubbed export, and attestation generation.
- Coverage gates must remain enabled for all Python packages; TypeScript coverage gates start when `packages/webui` lands.

## Repository Layout

```
/README.md  /ACRONYMS.md  /AGENTS.md  /CONTRIBUTORS.md  /LICENSE  /LICENSES/
/.github                    # CODEOWNERS, workflow callers, codecov, dependabot
/design                     # this documentation set
/docs                       # tutorial-style docs and future static site source
/pyproject.toml  /uv.lock  /.pre-commit-config.yaml   # Python workspace + local hooks
/packages
  /core-adapter             # session orchestration and event-streaming agent backend facade
  /gateway                  # REST/WS session gateway; owns the agent-server
  /compliance               # synthetic reviewer packet and audit bundle generation
  /adapters                 # adapter SPI protocols, conformance checks, and generic/local adapters
  /schemas                  # versioned policy, audit, detection, skill, confirmation, approval, and attestation schemas
  /session                  # command/event API shared by all interfaces
  /fixtures                 # synthetic fixture linting and no-live-data checks
  /model-policy             # policy profiles, capability tiers, attestation
  /detector                 # environment/dataset detection
  /skills                   # verification gate + heartwood.* metadata semantics
  /audit                    # hash-chained log + scrubbed export
  /adapters/{platform,model,data,registry}/*   # concrete adapter implementations
  /adapters/agent           # OpenHands agent-server binding behind the facade
  /mcp-servers              # data gateway, OMOP, FHIR, DRS, notebook
  /cli                      # command-line interaction surface
  /notebook                 # Python API + ipywidgets presentation adapter
  /webui                    # researcher web UI
/skills                     # bundle catalog + SKILL.md dirs (verified/ community/ experimental/)
/compliance                 # templates and generated reviewer packets
/images                     # generic and platform Dockerfiles
/evals                      # replay suites, skill evals, benchmark harness
/fixtures                   # synthetic data only
```

## Implementation Backlog

1. Keep the implemented 0A through 0F baseline green while landing the rest of 0G.
2. Land 0G as the next integration pass before frontend work.
3. Implement the `llama-cpp-cpu` profile, including the pinned runtime dependency, model artifact provenance, hash verification, license posture, resource limits, `linux/amd64` and `linux/arm64` support, and an offline load/query smoke test.
4. Pin production OpenHands runtime dependencies in the image after license and replay checks pass, then add an image smoke test that starts it behind the gateway and runs a CLI-gateway-agent-server turn.
5. Require explicit approval before every model invocation path and keep model prompt/response content out of persisted artifacts.
6. Keep optional GPU acceleration as a separate explicit runtime profile with Docker device configuration and GPU-capable CI rather than a baseline generic-image requirement.
7. Publish a self-contained static documentation site from checked-in project material after the real local-runtime path lands.
8. Keep the 0G Docker-only path reproducible from the published GitHub Container Registry image without requiring a repository checkout.
9. Land 0H after 0G by building the researcher web UI on the Spezi stack.
10. Add Server-Sent Events fallback, proxy smoke tests for `jupyter-server-proxy`, and runtime asset tests proving no external CDN or public network dependency.
11. Select and implement the first real platform pilot.

## Repository Strategy

- Keep this repository as the system of record through Phase 1.
- Publish signed generic and platform-specific images from this repository only after the generic image passes smoke tests.
- Publish the Phase 0 static documentation site from this repository through GitHub Pages once the local-runtime path is implemented, documented, and link-checked.
- Create a separate `heartwood-skills` repository only after local skill metadata, trust tiers, and skill eval harnesses are stable.
- Split platform adapters only when a platform requires a different release cadence, private validation fixtures, or platform-specific maintainers.
- Split large replay suites or benchmark data only when they become too large or too sensitive for the core repository.
- Do not create a separate marketplace, registry service, or platform-specific repository for Phase 0.

## Open Questions

- Which single platform is the first real-data pilot.
- Which group owns clinical/statistical review for the `verified` skill tier.
- What long-term funding and governance model supports maintenance.
- What exact cross-platform auth handshake binds each platform proxy identity to a gateway session key.
- Which tiny same-runtime CI model artifact and which more useful local tutorial model can satisfy the `llama-cpp-cpu` profile without violating licensing, redistribution, runtime-resource, architecture, or controlled-data constraints.
- What final project name should be used before public launch.

## Known Limitations

- Agentic-coding quality degrades on weak/local models; capability tiers and supervised paths mitigate but do not eliminate this risk.
- The sandbox network proxy is TLS-blind; platform egress-deny remains a required control.
- Dataset fingerprinting can mis-detect unusual schemas; propose-not-commit detection and human confirmation remain required.
- The current local-model path uses the `stub-loopback` deterministic fixture rather than LLM inference; the selected real profile is `llama-cpp-cpu` and still requires runtime dependency, model artifact, checksum, license, resource, and architecture validation.
- WebSocket reliability varies across platform proxies; Server-Sent Events fallback is required.
- The web UI adds a TypeScript supply-chain surface; pinned dependencies, Spezi shared configs, npm license gates, and CI checks are required.
