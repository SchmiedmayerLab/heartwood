<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 09 — Implementation plan

This document is the implementation checklist. It records the current repository baseline, the remaining Phase 0 passes, and the later phases. Architecture rationale belongs in [03](03-architecture.md), platform assumptions belong in [02](02-platforms.md), and development-tooling policy belongs in [08](08-development.md).

## Current Baseline

The repository is at **0G, 0I, and 0J complete; 0H remains the next implementation pass**. Passes 0A through 0G, 0I, and 0J are implemented and must stay green while the documentation-site, project-tracking, registry-governance, platform-proxy, larger-model, and controlled-platform work lands. The completed 0G slice proves the portable CPU local-runtime smoke path, keeps the deterministic stub as an explicit fixture profile, packages the pinned OpenHands agent-server command and tool stack, adds authenticated OpenHands-backed bash execution for the Docker-only offline stack, and publishes the multi-architecture image family from native architecture runners. The completed web UI and provider-invocation slices add the Spezi-based researcher interface, Server-Sent Events fallback, static asset serving, Jupyter proxy helpers, generic image web packaging, Terra-derived notebook image packaging, and policy-gated OpenAI-compatible provider invocation without changing the shared session command/event contract.

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
- The generic image family packages the Python workspace, CLI, gateway, notebook bridge, synthetic fixtures, verified skills, the deterministic loopback model stub, the pinned `llama-cpp-cpu` llama.cpp server binary, provider route validation, and the pinned OpenHands agent-server package.
- The `runtime` image flavor is the default platform-ready image, publishes as `edge` and model-explicit alias `edge-coder-7b`, bundles the pinned Qwen2.5-Coder-7B-Instruct Q4_K_M GGUF artifact, and is the default Docker demo image.
- The `smoke` image flavor publishes as `edge-smoke`, bundles the tiny verified GGUF model artifact, and proves offline local inference load/query behavior through the pinned llama.cpp `llama-server` binary in CI without making a model-quality or biomedical capability claim.
- The `providers` image flavor publishes as `edge-providers`, carries provider route configuration support with file-based runtime secret references, and does not bake provider secrets or model weights.
- The Terra-derived notebook image flavors publish as `edge-terra`, model-explicit alias `edge-terra-coder-7b`, and `edge-terra-smoke`, derive from `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6`, install a managed Python 3.12 Heartwood runtime under `/opt/heartwood`, register a Heartwood Jupyter kernel under `/opt/conda`, preserve the Terra base image entrypoint, bundle the 7B model in the default Terra runtime image, and publish user-facing Terra tags as single-platform Docker schema-2 manifests for Leonardo compatibility.
- The generic image pins the production OpenHands agent-server package, OpenHands tools package, and required tmux dependency, then starts the server as a gateway-owned localhost child during the offline stack run.
- The CLI supports an explicit `run --local-model` mode that invokes only allowlisted HTTP loopback endpoints, sends the submitted prompt to the in-boundary local model, records safe response metadata, and keeps response content out of persisted session events and audit exports by default; the local interactive demo can opt into a bounded response preview through `HEARTWOOD_DEMO_RESPONSE_PREVIEW=1` and defaults to a 768-token local model budget.
- The default 7B local-model image resource envelope is 4 vCPU, 16 GB RAM, and 25 GB free Docker disk for one active demo session, with 8 vCPU, 32 GB RAM, and 50 GB free Docker disk recommended for local Docker; Terra demos should use at least a 50 GB persistent disk and preferably 75 GB because the platform base, image layers, model artifact, extraction overhead, and workspace share the environment.
- The CLI can select a provider route from a validated provider configuration without reading provider secrets or invoking live provider APIs in synthetic tests.
- Docker Compose disables runtime network access for the smoke service and runs the offline stack script end to end.
- The offline stack smoke script starts the default `llama-cpp-cpu` runtime, runs detection, approves the synthetic model call, runs the agentic CLI through the local model endpoint, starts the gateway-managed OpenHands process during the agentic turn, writes a bounded synthetic workspace artifact through authenticated OpenHands bash execution, exports the scrubbed audit log, and generates the reviewer packet.
- The container image publish workflow builds `docker-bake.hcl` targets on native `linux/amd64` and `linux/arm64` runners on `main`, merges the generic architecture outputs into multi-architecture manifests, frees runner disk before the generic and real Terra model-image builds, publishes `edge`, `edge-coder-7b`, `edge-smoke`, `edge-providers`, `edge-terra`, `edge-terra-coder-7b`, `edge-terra-smoke`, and commit-SHA flavor tags to GitHub Container Registry, verifies every user-facing public tag through unauthenticated registry inspection, verifies generic tags resolve to both baseline Linux architectures, and verifies Terra tags through `images/platform/scripts/verify_registry_manifest.py` so they return `application/vnd.docker.distribution.manifest.v2+json` `linux/amd64` image manifests rather than OCI indexes until the selected Terra base and Leonardo support a broader manifest shape.
- `docs/getting-started-offline.md` documents pull-and-run usage from the published default 7B image, the smoke image CI path, and the local Compose workflow.
- `docs/container-images.md` records the image naming scheme, flavor policy, provider secret posture, local model strategy, and future GitHub Issues/Projects migration.
- The container smoke workflow runs the offline stack smoke on native GitHub-hosted `linux/amd64` and `linux/arm64` runners for pull requests, pushes to `main`, and manual dispatch. It also builds a lightweight Terra-compatible CI base, builds `edge-terra-smoke-ci` through the same platform Dockerfile on `linux/amd64`, runs the Terra platform image smoke, and runs the full offline stack smoke with runtime network disabled inside that CI image.
- Static image tests verify the generic image family includes the expected runtime surfaces, declares the local-runtime profiles, separates CPU and GPU runtime profiles, records the verified GGUF artifact, includes the OpenHands launcher, disables runtime network access through Compose, uses the smoke flavor for bundled model CI, publishes the expected image tags, validates provider route examples, covers both baseline Linux architectures, and records the Terra-derived image contract.

### Current Exclusions

- No static documentation site is published yet; tutorial and design documentation are checked in as Markdown.
- No real-platform adapter, proxy identity binding, or controlled-data validation exists yet.
- Terra, Seven Bridges, and DNAnexus proxy paths are documented as targets and covered by local `jupyter-server-proxy` style routing only; platform-native validation still requires platform workspaces.
- Autonomous OpenHands conversation turns driven by the local coding model are not yet proven; the current OpenHands path executes a bounded bash-backed workspace action after the Heartwood approval gate.
- The default bundled local model is Qwen2.5-Coder-7B-Instruct Q4_K_M and is suitable for first demos, but it is not a biomedical, production, or benchmark-backed quality claim.
- GPU acceleration is not active in the current default or Terra images; attached Terra GPUs do not speed up Heartwood local inference until a separate CUDA/vLLM/SGLang runtime profile, image target, device configuration, and GPU-capable tests are implemented.
- No high-memory or GPU bundled coding-model image target exists yet because the reviewed Qwen3-Coder-30B-A3B artifact still needs runtime profile, resource-envelope, CI, Terra VM, publication-cost, and live-demo review.
- No GPU runtime profile is implemented yet; CUDA support is documented as deferred.
- No automated GHCR tag cleanup exists yet; stale helper tags and unreferenced package versions require a documented dry-run cleanup policy before deletion.

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

## Completed Phase 0 Implementation Passes

### Implemented In 0G — End-to-End Local Runtime And Image Publication

- This pass proves the execution substrate the web UI and platform adapters will drive: local inference, the managed OpenHands agent-server, the gateway, the CLI, policy, audit, reviewer packet, image packaging, and CI.
- This pass stays synthetic-only; the local model test proves offline load, query, policy gating, event flow, and scrubbing, not biomedical quality or production coding quality.
- `images/generic/local-runtime/profiles.toml` declares `stub-loopback` as the implemented deterministic fixture and `llama-cpp-cpu` as the selected real local inference profile.
- The `llama-cpp-cpu` profile defines `linux/amd64` and `linux/arm64` support, CPU/GPU expectations, memory floor, localhost serving API, startup and shutdown behavior, failure modes, GGUF artifact policy, checksum policy, license posture, cache location, build-time resolution, runtime resolution, and CI expectations.
- `ggml-org/llama.cpp` release `b9937` is pinned as the CPU runtime dependency through architecture-specific Ubuntu binary archives for `linux/amd64` and `linux/arm64`, each verified by SHA-256 during the Docker build.
- The smoke image downloads `ggml-org/models-moved` `tinyllamas/stories260K.gguf` during image build, verifies byte size and SHA-256, and loads it at runtime without public network access.
- `openhands-agent-server==1.34.0` and `openhands-tools==1.34.0` are pinned for the Python 3.12 image path, and `images/generic/scripts/start_agent_server.sh` starts them only on loopback with temporary workspace paths.
- The session gateway can start a gateway-owned OpenHands agent-server child from environment configuration and blocks direct client endpoint exposure.
- `HEARTWOOD_AGENT_BACKEND=openhands-bash` selects an OpenHands-backed backend that lists registered OpenHands tools and writes a synthetic artifact through authenticated `/api/bash/execute_bash_command` after confirmation instead of the deterministic no-op.
- `HEARTWOOD_AGENT_BACKEND=local-workspace` remains available as a lightweight fallback backend for bounded local artifact writes without the OpenHands process.
- The offline smoke entrypoint starts the selected local runtime, invokes `heartwood run --local-model` through the approved loopback endpoint, starts the gateway-managed OpenHands child during the agentic turn, calls authenticated OpenHands `/api` routes, writes the synthetic workspace artifact, exports the scrubbed audit log, and generates the reviewer packet.
- The generic Dockerfile avoids baking API-key-like values into image `ARG` or `ENV`; the OpenHands smoke key is runtime-only and overridable.
- The Docker Compose smoke runs with an explicit non-root UID/GID, runtime network disabled, a read-only root filesystem, tmpfs write points, dropped Linux capabilities, `no-new-privileges`, and a process limit.
- CI runs Buildx Dockerfile checks before the container smoke path so secret-like `ARG` or `ENV` warnings fail in pull requests.
- CI runs the `linux/arm64` offline smoke on a native GitHub-hosted ARM runner instead of QEMU runtime emulation, while keeping the required check name `Offline stack smoke test (linux/arm64)`.
- Main-branch image publication builds each architecture on a native GitHub-hosted runner and creates the public multi-architecture image tags through a final manifest merge job.
- `docker-bake.hcl` defines `runtime`, `smoke`, and `providers` image targets from one Dockerfile.
- Main-branch image publication pulls the current base image tag and uses BuildKit cache. Generic GitHub Container Registry image flavors keep SBOM and provenance attestations; Terra-facing tags disable Buildx attestations and force Docker schema-2 media types because Terra Leonardo image auto-detection rejects OCI indexes. The platform registry verifier reads `images/platforms.toml`, follows registry Bearer challenges, verifies runtime, coder-alias, and smoke moving/commit tags, and enforces the configured manifest media type, config media type, supported platforms, and non-platform manifest policy.
- `images/generic/image-flavors.toml` records the `edge`, `edge-coder-7b`, `edge-smoke`, `edge-providers`, and commit-SHA tag scheme and reserves `latest` for a future stable release.
- `images/generic/providers/provider-routes.example.toml` records provider route examples for OpenAI-compatible local endpoints, OpenAI, Azure OpenAI, Anthropic, Vertex AI, and Bedrock using file-based runtime secrets or managed identity only.
- `packages/gateway` validates provider route configuration, rejects inline secrets, enforces absolute secret-file paths, normalizes endpoints, and exposes only non-secret route metadata.
- `images/generic/local-runtime/model-catalog.toml` records the implemented smoke model, implemented Qwen2.5-Coder-7B default demo model, and deferred high-memory coding-model candidates including Qwen3-Coder-30B-A3B.
- `llama-cpp-cuda` is recorded as a deferred optional NVIDIA acceleration profile, separate from the portable CPU baseline and requiring explicit host GPU runtime support.
- Static tests verify the runtime profile, model manifest, model catalog, provider route examples, launcher routing, Docker network-off smoke command, multi-architecture smoke matrix, OpenHands launcher, Bake targets, and published image tags.

**Completed verification**

- A documented Docker-only command can pull the `edge` image and run the interactive local coding-model demo; a separate documented command can pull the `edge-smoke` image and run the offline local-stack smoke test without public runtime network access.
- The smoke test starts a real local inference runtime, runs a model call after explicit approval, starts the managed OpenHands agent-server behind the gateway, completes one synthetic agentic CLI turn with authenticated OpenHands bash execution, exports a scrubbed audit log, and generates the synthetic evidence bundle.
- The same session command/event contract drives the CLI, notebook bridge, gateway, and OpenHands backend path.
- The default `edge` runtime image bundles the pinned Qwen2.5-Coder-7B Q4_K_M artifact; the `edge-smoke` image bundles only the tiny verified smoke artifact for CI; provider secrets are runtime file or identity inputs only.
- Prompt and response content are not persisted in commands, audit exports, reviewer packets, or CI logs; session events store response content only when the local synthetic demo explicitly enables the bounded response preview.
- GitHub Actions covers the image smoke, local inference runtime profile, OpenHands behind-gateway path, and GHCR publishing path.
- The generic image family publishes multi-architecture manifests for `edge`, `edge-coder-7b`, `edge-smoke`, and `edge-providers` on `linux/amd64` and `linux/arm64`; any architecture excluded from a runtime profile is explicitly documented in the profile.

### Implemented In 0I — Researcher Web UI And Platform Surfacing

- `packages/webui` contains a standalone TypeScript single-page app built on `@stanfordspezi/spezi-web-design-system`, `@stanfordspezi/spezi-web-configurations`, React, Vite, Vitest, and Playwright.
- The web UI renders gateway events as a conversation transcript with UI-local user prompt turns, bounded synthetic model response previews when enabled, highlighted agent messages, compact event-derived trace summaries, dataset proposals, approval controls with approve/deny actions, policy status, provider route metadata, local model invocation status, activity trace, and scrubbed audit export links.
- The web UI uses WebSocket streaming as the primary transport, falls back to Server-Sent Events, and rehydrates by replaying persisted session events after reconnect.
- The web client uses relative assets, infers `jupyter-server-proxy` API bases such as `/proxy/8767/`, and also supports an explicit gateway base through build-time configuration.
- `packages/gateway` serves self-contained static web assets, accepts gateway REST/WebSocket/Server-Sent Events routes under the configured proxy base path, rejects `/sessions/*` static fallbacks, and exposes `GET /sessions/{session}/events/stream` as the Server-Sent Events fallback.
- The notebook bridge exposes `web_proxy_url()` and `jupyter_proxy_url()` helpers for Jupyter-style proxy routes and projects provider route metadata into notebook policy status.
- The CLI exposes `heartwood serve` to run the gateway and packaged web UI from one command.
- `images/generic/scripts/start_demo_stack.sh` starts the self-contained local Docker demo path from the default 7B runtime image with the bundled local model, seeded synthetic model-call approval, gateway-managed OpenHands child server, 768-token bounded response preview, and packaged web UI. `images/generic/scripts/start_web_ui.sh` remains the lower-level gateway/web UI launcher with runtime-configurable workspace, host, port, web root, and base path.
- The generic Dockerfile builds the web UI in a Node 24 stage and copies only static `dist` assets into the final Python runtime image; `node_modules` is not present in the final runtime layer.
- The generic Dockerfile copies `README.md`, `ACRONYMS.md`, `docs/`, and `design/` into `/opt/heartwood` so the packaged runtime includes the tutorial notebook and design record without requiring a repository checkout.
- `images/platform/Dockerfile`, `images/platforms.toml`, `images/platform/scripts/verify_registry_manifest.py`, and `docker-bake.hcl` define an extensible platform-derived image layer. The implemented Terra targets use the Terra Jupyter Python base, install the Heartwood runtime under `/opt/heartwood`, register the `heartwood` Jupyter kernel, keep the Terra base entrypoint, bundle the 7B model in the default Terra runtime image, and publish `edge-terra`, `edge-terra-coder-7b`, and `edge-terra-smoke` on `linux/amd64` as Docker schema-2 image manifests for Leonardo compatibility.
- `images/platform/terra-ci-base.Dockerfile` defines a lightweight Terra-compatible notebook base for pull-request smoke tests; it is not a user-facing Terra image and exists to test the platform Dockerfile, Jupyter home/prefix assumptions, kernel registration, packaged UI, and offline stack without pulling the real Terra base on every pull request.
- `images/platform/scripts/terra_image_smoke.sh` verifies that the Terra image contains the Heartwood CLI, notebook bridge, tutorial docs, web UI assets, Jupyter kernel registration, and platform home assumptions before the full offline stack smoke runs.
- The web UI dependency tree is pinned by `package-lock.json`, checked by ESLint, Prettier, strict TypeScript, Vitest coverage, Playwright browser smoke tests, npm audit, and an npm license compatibility check.
- The `Web UI` GitHub Actions workflow runs the TypeScript, unit, coverage, build, license, audit, browser smoke, and gateway-served proxy smoke checks on pull requests and `main`.
- Documentation records local Docker serving and Jupyter proxy serving through the packaged web UI.
- `docs/terra-jupyter-demo.ipynb` provides a runnable synthetic Jupyter walkthrough for Terra-like workspaces, including proxy URL calculation, notebook API detection, synthetic workflow execution, approval/activity inspection, audit export, CLI replay comparison, and packaged web UI launch instructions.

**Completed verification**

- Web UI unit tests cover event projection, conversation transcript rendering, run payload assembly, replay rehydration, command error rendering, WebSocket streaming, and Server-Sent Events fallback cleanup.
- Playwright loads the built app through Vite preview and verifies that mocked gateway events render through the researcher UI.
- Gateway tests cover REST, WebSocket, Server-Sent Events replay, and static asset serving under a proxy base path.
- The gateway-served proxy smoke test starts `heartwood serve`, loads the built web UI under `/proxy/<port>/`, and exercises prefixed command and replay routes.
- Static image tests verify that the Dockerfile builds and copies web assets, tutorial docs, and design docs, and that the image includes the web UI launcher.
- Static image tests verify the platform image manifest, Terra base image, Terra tags, Jupyter kernel registration, no baked secrets, and `linux/amd64`-only Terra publication contract.
- Static documentation asset tests verify that the Terra Jupyter notebook remains synthetic-only, has no saved outputs, covers the CLI, notebook API, audit export, widget projection, offline stack smoke, and packaged web UI launcher paths, and clearly states that live Terra workspace validation remains required.

### Implemented In 0J — Provider Invocation

- Provider route configuration and invocation support live in `packages/adapters`, keeping provider behavior behind the model adapter boundary instead of the gateway owning provider-specific logic.
- Provider route validation rejects inline secrets, requires absolute `secret_file` paths for secret-bearing routes, validates provider ids, auth modes, capability tiers, endpoint normalization, duplicate route ids, and default route references.
- Provider invocation supports OpenAI-compatible chat-completions routes, including local loopback, OpenAI, Azure OpenAI, llama.cpp, and vLLM routes, using content-free synthetic messages and bounded timeouts.
- Secret-bearing routes read runtime secret files only after policy approval and selected-route validation; secret paths and secret values are not recorded in session events, audit exports, reviewer packets, or Docker image layers.
- Managed-identity routes are valid configuration metadata but reject invocation until a real platform adapter supplies the identity exchange.
- `heartwood run --provider-config --provider-route --invoke-provider` invokes a selected provider route only after the model-call decision is allowed and a `model-call` approval for that decision exists.
- Local model invocation and provider route invocation are mutually exclusive for one run command.
- Provider route decisions record only safe route metadata, response metadata, and attestations by default; prompt and response content remain scrubbed from exported artifacts, while the synthetic local demo can opt into a bounded response preview for UI demonstration.
- The notebook API mirrors provider route selection and invocation flags through the shared session command payload.

**Completed verification**

- Provider route tests cover successful OpenAI-compatible invocation against a local synthetic server, secret-file authorization, managed-identity rejection, malformed routes, duplicate route ids, inline secret rejection, and adapter invocation conformance.
- Session service tests cover approval-before-invocation, denied provider routes, missing secret files not being read on policy denial, provider response metadata, and provider/local-model mutual exclusion.
- CLI tests cover provider route selection, `--invoke-provider` validation, and `heartwood serve`.

## Remaining Phase 0 Passes

### 0H — Static Documentation Site And Project Tracking

**Fit and sequencing**

- Land this pass next. It was intentionally deferred while the web UI, provider invocation, Terra-derived platform image, and published-image manifest work reached an end-to-end shape.
- Keep this pass documentation- and governance-focused; do not add new runtime surfaces, new platform adapters, live controlled-platform validation, autonomous larger-model execution, or larger model image targets here.
- Use only checked-in project material as site source content, and keep the generated site free of external runtime dependencies.
- Preserve the completed CLI, notebook, web UI, provider-route, generic image, Terra image, offline smoke, and reviewer-packet behavior as the evidence base for the published documentation.

**Required work**

- Add a static documentation site build that renders the README, getting-started guide, container image guide, design documents, acronym glossary, project goals, current limitations, security posture, and contribution routing from checked-in Markdown.
- Configure GitHub Pages publication from `main` only, using least-privilege workflow permissions and no repository secrets.
- Add a pull-request documentation build job that fails on broken internal links, broken checked-in asset references, malformed Markdown where the selected tool can detect it, missing SPDX headers in new source files, and external CDN dependencies.
- Add site navigation that exposes the offline Docker tutorial, Terra-style Jupyter demo, platform image extension mechanism, image flavor policy, provider secret policy, local model strategy, architecture, security/compliance model, audit/reviewer-packet flow, testing/evaluation plan, development workflow, and implementation plan.
- Make the site build reproducible locally from the repository without requiring published images, cloud credentials, or network access after dependencies are installed.
- Update reviewer-packet limitations so they describe the static site as the current documentation pass and acknowledge that the web UI, Server-Sent Events fallback, Jupyter-style proxy smoke, and Terra-style packaged demo smoke are already implemented synthetic paths.
- Formalize the container tag lifecycle in documentation and CI: public `edge`, `edge-coder-7b`, `edge-smoke`, `edge-providers`, commit-SHA, and future semver tags are unified multi-platform image indexes; architecture-specific tags are internal assembly details, not user-facing install targets.
- Keep post-publish registry verification for every public image tag: generic unified tags must resolve to `linux/amd64` and `linux/arm64`, while platform-derived tags must be checked by the platform registry verifier and match the platform manifest's supported architecture set, manifest media type, config media type, tag templates, and non-platform manifest policy.
- Replace architecture-helper tags with digest-based manifest assembly where feasible; otherwise keep helper tags explicitly internal and define a retention policy that preserves manifests referenced by public indexes.
- Define a GHCR cleanup policy that keeps current moving tags, semver release tags, a bounded history of commit-SHA tags, SBOM/provenance artifacts, and any digest referenced by public multi-platform indexes, while cleaning stale helper tags and unreferenced package versions only after a dry-run report.
- Move operational implementation items from long Markdown backlog lists into GitHub Issues and a GitHub Project after the site structure is stable; use fields for phase, risk, platform, owner, status, required evidence, and dependency.
- Keep the Markdown design documents as canonical architecture records, and link from the static site and GitHub Project back to the owning design sections.

**Required tests**

- Documentation site build runs in CI on pull requests and `main`.
- Link checks cover README, docs, design documents, and generated site routes.
- Static asset checks prove no external CDN or public network dependency is required at runtime.
- GitHub Pages publish workflow runs only from `main` and uses read-only contents permissions plus the minimum Pages deployment permissions.
- Reviewer-packet tests assert that current limitations no longer name 0G as blocked by the documentation site.
- Image publication checks inspect public unified tags with an unauthenticated Docker config after manifest creation and fail if a tag is private, any required generic platform is missing, or any platform-specific tag violates its registry manifest contract.
- Registry cleanup tests run in dry-run mode and assert that current moving tags, semver tags, retained commit-SHA tags, and manifests referenced by public indexes are never selected for deletion.

**Exit gates**

- The static documentation site is available from GitHub Pages and matches the checked-in README, tutorial, design documents, goals, and limitations.
- The site can be built locally from a checkout with one documented command.
- CI blocks broken links, missing generated-site routes, and external runtime assets.
- Public image references use unified multi-platform generic tags, platform-compatible tags that pass the platform manifest checks, or explicit digests; no user-facing documentation instructs users to pull architecture-helper tags.
- The publish workflow verifies `edge`, `edge-coder-7b`, `edge-smoke`, `edge-providers`, and retained commit-SHA tags as multi-platform indexes for `linux/amd64` and `linux/arm64`.
- GHCR tag cleanup has a documented policy, dry-run output, required permissions, retention window, protected tag patterns, and rollback procedure.
- A GitHub Project exists for implementation tracking, and remaining operational backlog items have issues with phase, risk, platform, owner, status, required evidence, and dependency metadata.

### 0K — Larger Local Model Evaluation And Platform Proxy Validation

**Fit and sequencing**

- Start this pass after the generic UI and provider invocation surfaces are merged.
- Keep larger local models and optional GPU acceleration out of required pull-request CI until artifact size, runtime, licensing, and runner capacity are reviewed.
- Preserve the 0G safety baseline: explicit approval before model invocation, policy-gated endpoints, loopback-only local runtime defaults, scrubbed persisted artifacts, and gateway-owned agent-server access.

**Required work**

- Route any OpenHands model access through the gateway policy layer; the agent-server must not hold a separate public model egress path.
- Prove an autonomous OpenHands conversation turn against a larger local tutorial coding model without weakening approval, audit, scrubbed-event, or capability-tier controls.
- Extend the implemented Qwen2.5-Coder-7B default model path from first demo output to a measured agentic coding workflow with prompt templates, response budget controls, latency recording, replay comparison, and approval/audit evidence.
- Evaluate Qwen3-Coder-30B-A3B or a comparable high-capability local coding model as a separate high-memory or GPU image only after source revision, license posture, redistribution allowance, quantization, byte size, SHA-256, provenance, cache location, build-time versus runtime resolution, CPU/GPU memory envelope, Terra VM shape, and architecture support are recorded.
- Validate Terra image launch, notebook startup, `/home/jupyter` workspace behavior, GitHub Container Registry or Google Artifact Registry image access, image size/startup timing, `jupyter-server-proxy` path behavior, and web UI conversation interaction with user prompt, model preview, agent message, and trace summary in a synthetic Terra workspace before documenting Terra as supported in live Terra.
- Reuse the `images/platform/Dockerfile` and `images/platforms.toml` pattern for Seven Bridges and DNAnexus only after their base image, home directory, proxy path, registry access, and identity headers are validated.
- Validate Terra and Seven Bridges proxy paths through their Jupyter or Data Studio routes.
- Validate DNAnexus first through `jupyter-server-proxy`; keep `httpsApp` as the platform-native upgrade path.
- Inherit identity from the platform proxy; do not add a Heartwood-owned login.
- Keep optional GPU acceleration behind a separate runtime profile, Docker device configuration, and self-hosted GPU or scheduled platform checks; do not make it a requirement for the generic CPU image.

**Required tests**

- OpenHands model-routing tests prove the agent-server cannot reach model endpoints except through the gateway policy layer.
- Terra image tests statically verify the selected Terra base image, `/home/jupyter` assumptions, packaged Heartwood docs/notebook, loopback web UI port, Jupyter service path, Jupyter kernel registration, publication tags, and absence of baked provider secrets.
- Platform proxy smoke tests cover Terra, Seven Bridges, and DNAnexus route prefixes when suitable test workspaces are available.
- A live Terra workspace smoke records the custom image digest, Terra base image digest, VM shape, notebook startup result, web UI proxy URL shape, one synthetic web UI conversation command with user prompt, model preview, agent message, and trace summary, CLI replay count, audit export path, reviewer packet path, and any identity/proxy headers exposed to Heartwood.
- Larger-model and autonomous-coding tests run as optional scheduled or release checks until runtime and cost are acceptable for pull-request CI.
- Image tests verify high-memory or GPU model targets are separate from the default `edge`, `edge-coder-7b`, `edge-smoke`, and `edge-providers` tags.
- GPU tests run only on explicit GPU-capable runners and are not baseline merge gates.

**Exit gates**

- OpenHands autonomous model access is mediated by the gateway and respects capability-tier limits.
- The web UI is validated behind at least one real controlled-platform proxy route.
- A Terra-derived Heartwood notebook image launches in Terra from a documented registry reference and completes the synthetic local-model, OpenHands, CLI, notebook, web UI, audit, and reviewer-packet demo path.
- The optional larger coding-model target has reviewed provenance, licensing, checksums, resource envelopes, and CI scope.
- Baseline CPU images and pull-request CI remain portable on native `linux/amd64` and `linux/arm64` runners.

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
- Expand local-model support beyond the Phase 0 smoke profile under explicit supervised capability tiers.
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

1. Keep the implemented 0A through 0G, 0I, and 0J baseline green while adding documentation, larger-model, and platform work.
2. Land 0H next by publishing the static documentation site, adding documentation CI, updating reviewer-packet limitations, formalizing GHCR tag cleanup, and moving operational tracking into GitHub Issues and a GitHub Project.
3. Keep the implemented `llama-cpp-cpu` profile covered by the pinned llama.cpp server binary, model artifact provenance, hash verification, license posture, resource limits, `linux/amd64` and `linux/arm64` support, and offline load/query smoke test.
4. Keep `edge`, `edge-coder-7b`, `edge-smoke`, `edge-providers`, commit-SHA flavor tags, and any required temporary architecture build outputs reproducible through `docker-bake.hcl` and the native-runner publish workflow.
5. Verify public image tags as unified multi-platform indexes, move away from permanent architecture-helper tags where feasible, and add a GHCR cleanup policy with protected tag patterns and dry-run checks.
6. Keep the implemented web UI as a presentation adapter over the gateway; do not add browser-only policy or a second session contract.
7. Keep Server-Sent Events fallback, browser smoke tests, preserved-prefix gateway smokes, stripped `jupyter-server-proxy` route tests, packaged-runtime Terra/Jupyter demo smoke, and runtime asset tests green as the UI expands.
8. Keep provider invocation behind validated provider routes and the gateway policy layer without storing provider secrets in image layers or persisted artifacts.
9. Extend the OpenHands-backed path from bounded bash execution to an autonomous conversation turn with the default 7B local coding model after replay and policy checks pass.
10. Select, pin, and review the first larger coding-model artifact before implementing the optional bundled coding-model image target.
11. Keep the implemented Terra-derived notebook image publish and CI smoke path green, and validate it in a live Terra workspace before claiming Terra custom-environment support; the generic image remains the portable runtime baseline.
12. Validate Terra, Seven Bridges, and DNAnexus proxy paths in real platform workspaces before claiming platform proxy support.
13. Require explicit approval before every model invocation path and keep model prompt/response content out of persisted artifacts.
14. Keep optional GPU acceleration as a separate explicit runtime profile with Docker device configuration and GPU-capable CI rather than a baseline generic-image requirement.
15. Keep the 0G Docker-only path reproducible from the published GitHub Container Registry smoke image without requiring a repository checkout.
16. Select and implement the first real platform pilot only after synthetic replay, documentation, web UI, provider policy, and reviewer-packet controls are green.

## Repository Strategy

- Keep this repository as the system of record through Phase 1.
- Publish signed generic and platform-specific images from this repository only after the generic image passes smoke tests.
- Publish the Phase 0 static documentation site from this repository through GitHub Pages during 0H.
- Keep Markdown design docs as canonical architecture records; move operational execution tracking to GitHub Issues and a GitHub Project during 0H while linking issues back to the owning design sections.
- Create a separate `heartwood-skills` repository only after local skill metadata, trust tiers, and skill eval harnesses are stable.
- Split platform adapters only when a platform requires a different release cadence, private validation fixtures, or platform-specific maintainers.
- Split large replay suites or benchmark data only when they become too large or too sensitive for the core repository.
- Do not create a separate marketplace, registry service, or platform-specific repository for Phase 0.

## Open Questions

- Which single platform is the first real-data pilot.
- Which group owns clinical/statistical review for the `verified` skill tier.
- What long-term funding and governance model supports maintenance.
- What exact cross-platform auth handshake binds each platform proxy identity to a gateway session key.
- Which more useful local tutorial model can extend the `llama-cpp-cpu` profile without violating licensing, redistribution, runtime-resource, architecture, or controlled-data constraints.
- What final project name should be used before public launch.

## Known Limitations

- Agentic-coding quality degrades on weak/local models; capability tiers and supervised paths mitigate but do not eliminate this risk.
- The sandbox network proxy is TLS-blind; platform egress-deny remains a required control.
- Dataset fingerprinting can mis-detect unusual schemas; propose-not-commit detection and human confirmation remain required.
- The current local-model path uses a tiny `llama-cpp-cpu` smoke artifact that proves local load/query behavior but does not provide production coding-agent or biomedical reasoning quality.
- The OpenHands agent-server package, gateway-owned process boundary, and bounded bash execution path are implemented, but autonomous coding quality with a larger local tutorial model still needs validation.
- Terra custom-environment support is not complete until a Terra-base-derived Heartwood notebook image launches in Terra and completes the synthetic local-model, web UI, CLI, notebook, audit, and reviewer-packet smoke in a live Terra workspace.
- WebSocket reliability varies across platform proxies; Server-Sent Events fallback is required.
- The web UI adds a TypeScript supply-chain surface; pinned dependencies, Spezi shared configs, npm license gates, and CI checks are required.
