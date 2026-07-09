<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 09 — Implementation plan

This document is the implementation checklist. It records the current repository baseline, the remaining Phase 0 passes, and the later phases. Architecture rationale belongs in [03](03-architecture.md), platform assumptions belong in [02](02-platforms.md), and development-tooling policy belongs in [08](08-development.md).

## Current Baseline

The repository is at **0D — Prototype skills and replay**. The baseline includes passes 0A through 0D and must stay green while later passes are added.

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

### Current 0D Exclusions

- No REST/WebSocket gateway package exists yet.
- No OpenHands agent-server binding exists yet.
- No LiteLLM egress proxy exists yet.
- CLI support beyond detection is not complete.
- No notebook API or widget bridge exists yet.
- No generic Docker image or Docker Compose smoke test exists yet.
- No reviewer packet generator exists yet.
- No TypeScript web UI exists yet.
- No real-platform adapter or controlled-data validation exists yet.

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

### 0E — Gateway And Agent-Server Binding

**Required work**

- Add `packages/gateway`.
- Implement REST commands over the existing session command contract.
- Implement WebSocket streaming over the existing session event contract.
- Add Server-Sent Events support only if needed for gateway-level streaming tests; web UI fallback is completed in 0G.
- Own the OpenHands agent-server as a managed child process.
- Bind the agent-server to localhost only.
- Block direct client access to the agent-server; all commands and streamed events must pass through the gateway.
- Bind `openhands-agent-server` behind the core-adapter facade after dependency versions, policy gates, and replay behavior are pinned.
- Run the agent-server in the non-Docker Local runtime inside the platform container; the sandbox boundary is bubblewrap plus platform egress-deny.
- Add the model-policy egress proxy in front of LiteLLM so the policy decision gates actual model calls and the agent-server never reaches model endpoints directly.
- Preserve the deterministic in-process service path for offline commands and tests.

**Required tests**

- Gateway contract tests for REST command handling, WebSocket event streaming, reconnect replay, pause/resume, confirmation request/resolution, policy denial, audit persistence, and malformed command errors.
- Fake-agent-server tests proving translation between OpenHands-style events and Heartwood session events.
- Localhost-binding tests proving the agent-server is reachable only through the gateway path.
- Egress-proxy tests proving denied model calls do not reach LiteLLM or provider endpoints.

**Exit gates**

- Gateway tests pass without network access.
- Existing 0D deterministic tests remain green.
- The agent-server binding can be disabled for offline CI.
- No surface can bypass the gateway to reach the agent-server.

### 0F — CLI, Notebook Bridge, Image, And Reviewer Packet

**Required work**

- Expand the CLI to support `heartwood detect`, `heartwood chat`, `heartwood run`, `heartwood replay`, and `heartwood audit export`.
- Make `heartwood chat` an interactive terminal session with chat turns, visible tool/code events, approve/deny prompts, pause/resume, replay, and audit export.
- Add CLI transcript or snapshot tests generated from synthetic session events.
- Add `packages/notebook` with a Python API and minimal `ipywidgets` bridge.
- Notebook widgets must render chat, activity, dataset proposals, skill proposals, approval controls, policy status, and export actions.
- Build one self-contained generic Linux/Jupyter image with gateway, agent-server Local runtime, CLI, notebook bridge, synthetic fixtures, verified skills, and all runtime dependencies.
- Add Docker Compose smoke tests for the generic image.
- Generate a scrubbed audit bundle and egress-attestation report from the synthetic workflow.
- Add a reviewer packet generator.
- Reviewer packet contents must include threat model summary, data-flow diagram, policy profile, fixture statement, sample audit log, sample attestation, dependency/license summary, and current limitations.

**Required tests**

- CLI command tests and interactive transcript snapshots.
- Notebook view-model tests generated from the same session events as the CLI.
- Docker Compose smoke tests for the generic image.
- Audit bundle and attestation golden-output tests.
- Reviewer packet generation tests that use checked-in synthetic fixtures only.

**Exit gates**

- The synthetic workflow runs end-to-end from the CLI in the generic image with no external network access.
- The notebook bridge observes the same gateway session events as the CLI.
- The reviewer packet is generated from synthetic fixtures only.
- Container smoke tests pass in CI.

### 0G — Researcher Web UI And Platform Surfacing

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

- The full synthetic workflow runs from the web UI on the generic image.
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
/pyproject.toml  /uv.lock  /.pre-commit-config.yaml   # Python workspace + local hooks
/packages
  /core-adapter             # session orchestration and event-streaming agent backend facade
  /gateway                  # REST/WS session gateway; owns the agent-server
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

1. Keep the current 0D baseline green while landing later passes.
2. Add the REST/WebSocket gateway.
3. Bind the OpenHands agent-server behind the facade in a Local runtime.
4. Add the model-policy egress proxy in front of LiteLLM.
5. Expand the CLI into an interactive surface.
6. Add the notebook API and widget bridge.
7. Package the generic Docker image and add Docker Compose smoke tests.
8. Add audit bundle, attestation, and reviewer packet generation.
9. Build the researcher web UI on the Spezi stack.
10. Add proxy surfacing and smoke tests for `jupyter-server-proxy`.
11. Select and implement the first real platform pilot.

## Repository Strategy

- Keep this repository as the system of record through Phase 1.
- Publish signed generic and platform-specific images from this repository only after the generic image passes smoke tests.
- Create a separate `heartwood-skills` repository only after local skill metadata, trust tiers, and skill eval harnesses are stable.
- Split platform adapters only when a platform requires a different release cadence, private validation fixtures, or platform-specific maintainers.
- Split large replay suites or benchmark data only when they become too large or too sensitive for the core repository.
- Publish a documentation site only when external user documentation becomes release-managed.
- Do not create a separate marketplace, registry service, or platform-specific repository for Phase 0.

## Open Questions

- Which single platform is the first real-data pilot.
- Which group owns clinical/statistical review for the `verified` skill tier.
- What long-term funding and governance model supports maintenance.
- What exact cross-platform auth handshake binds each platform proxy identity to a gateway session key.
- What final project name should be used before public launch.

## Known Limitations

- Agentic-coding quality degrades on weak/local models; capability tiers and supervised paths mitigate but do not eliminate this risk.
- The sandbox network proxy is TLS-blind; platform egress-deny remains a required control.
- Dataset fingerprinting can mis-detect unusual schemas; propose-not-commit detection and human confirmation remain required.
- WebSocket reliability varies across platform proxies; Server-Sent Events fallback is required.
- The web UI adds a TypeScript supply-chain surface; pinned dependencies, Spezi shared configs, npm license gates, and CI checks are required.
