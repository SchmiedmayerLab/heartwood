<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 09 — Implementation plan

Delivery is phased by capability. Phase 0 proves one generic, synthetic-data vertical slice before platform breadth: CLI session → detected environment → confirmed skill → sandboxed Python analysis → aggregate result → policy/audit record → attestation. Notebook UI is a presentation adapter over the same session model.

## Planning principles

- **One core repository first.** Keep the core harness, generic adapters, schemas, prototype skills, fixtures, evals, image, and documentation together until the public contracts stabilize.
- **Synthetic first.** No controlled data is used before the generic replay suite, policy gates, reviewer packet, and container smoke tests are passing.
- **Extension by contract.** Every adapter, skill, policy profile, and data-source integration has a typed interface, a schema, and conformance tests.
- **CLI first.** The command-line interface is the main product surface, the development interface, and the stable CI target. Notebook widgets consume the same commands and event stream.
- **CI as part of the product.** Repository validation, type checks, unit tests, replay tests, no-live-data checks, and container smoke tests are required branch checks from the first implementation.
- **Evidence over claims.** Security and compliance claims are backed by generated artifacts: denied-egress tests, count-floor tests, sample audit logs, and sample attestations.

## Phase 0 — Core implementation prototype

The platform-agnostic core runs end-to-end in a plain Linux/Jupyter environment on synthetic OMOP-like data.

### 0A — Repository bootstrap and CI baseline

- Add repository-local health files: `LICENSE`, `LICENSES/`, `CONTRIBUTORS.md`, `.github/CODEOWNERS`, `.gitignore`, `.pre-commit-config.yaml`, `.yamllint.yml`, `.linkspector.yml`, `.github/codecov.yml`, and `.github/dependabot.yml`. Inherit the shared community health files (code of conduct, contributing guide, security policy, support, funding, and issue/PR templates) from the `SchmiedmayerLab/.github` organization repository rather than duplicating them.
- Add a GitHub ruleset for the default branch: required PR review, CODEOWNERS review for owned paths, required status checks, linear history, and no direct pushes.
- Add a repo-local `.github/workflows/validate.yml` orchestrator that mirrors the lab pattern: REUSE, actionlint, Markdown links, yamllint, and whitespace.
- Call reusable `SchmiedmayerLab/.github` workflows for `reuse.yml`, `actionlint.yml`, and `markdown-links.yml`. Run yamllint and whitespace locally because the shared `validate.yml` is not a reusable workflow.
- Add the Python workspace skeleton (`pyproject.toml`, `uv.lock`, the `packages/` layout, and lint/type/test config) with one passing placeholder test, and wire `pre-commit` to mirror the validate and Python checks so failures surface locally.
- Add repo-local Python CI using `uv sync --locked`, `ruff format --check`, `ruff check`, strict `mypy` or `pyright`, `pytest`, and Codecov upload.
- Scaffold the container smoke workflow that builds the generic image and runs one synthetic end-to-end command through Docker Compose; it becomes a required check once the generic image lands in 0E.
- Set workflow permissions to least privilege. Use OIDC only for artifact publishing, avoid repository secrets in pull-request workflows, and isolate untrusted fork runs.
- Add secret scanning and dependency review gates before publishing images.
- Add Dependabot groups for GitHub Actions, Python manifests, Node manifests if present, and container base images once Dockerfiles exist.

### 0B — Contracts, schemas, and fixtures

- Define `PlatformAdapter`, `ModelProviderAdapter`, `DataSourceAdapter`, and `RegistryAdapter` protocols.
- Define versioned schemas for policy profiles, model-call decisions, egress-attestation records, audit events, detector evidence, and `heartwood.*` skill metadata.
- Define a session command/event contract shared by the CLI, notebook API, and future UI surfaces.
- Add synthetic fixtures for environment probes, OMOP-like tables, denied egress attempts, skill metadata, approval records, and expected audit exports.
- Add conformance tests that every adapter implementation must pass.
- Add fixture linting that rejects live identifiers, PHI-shaped values, secrets, and non-synthetic source markers.

**Completion criteria:** the adapter protocols import cleanly, schema models validate representative fixture records and reject malformed records, the session command/event contract serializes to stable JSON-facing names, adapter conformance checks pass against deterministic fake implementations, the no-live-data fixture linter passes on checked-in synthetic fixtures and fails on seeded direct identifiers, secrets, and live-source markers, and the standard repository checks remain green.

### 0C — Core harness

- Add an SDK-neutral agent facade for sessions, tool execution summaries, event logging, and replay; keep the first backend deterministic and offline, and add the real OpenHands binding only after dependencies, policy gates, and replay behavior are pinned.
- Build the session service that accepts commands, emits structured events, and persists resumable state.
- Provide the `generic` platform adapter, `local-fs` data adapter, and a deterministic fake/local model provider for tests.
- Keep platform and dataset detection deterministic with propose-not-commit behavior, confidence scores, visible evidence, and logged human approval.
- Enforce the model policy layer with deny-egress defaults, exact normalized endpoint matching, malformed-endpoint deny behavior, capability-tier checks, credential filtering, and attestation records.
- Persist session state and the hash-chained audit log on the workspace disk, verify the chain before export, and write scrubbed JSONL exports that omit prompt, response, row, and value payloads.
- Keep local data access root-confined and deterministic; dataset fingerprints may use filenames and headers, but not row values.
- Do not load skills, perform registry network lookups, or execute model calls implicitly before verification and explicit approval are represented in the session event stream.

**Completion criteria:** the deterministic session service handles detection, approvals, model-call policy decisions, no-op tool execution, replay, and audit export through the shared command/event contract; generic/local adapters pass the conformance suite; model-policy tests prove exact endpoint allowlisting, malformed endpoint denial, capability-tier denial, and credential allowlisting; data-adapter tests prove root confinement and header-only fingerprinting; audit tests prove hash-chain verification, tamper detection, and scrubbed export; CLI detection runs through the session service and persists local state without network access; and the standard repository checks remain green.

### 0D — Prototype skills and replay

- Implement the skill verification gate for local `SKILL.md` directories: metadata schema, trust tier, signature placeholder, network requirement, declared tools, approval copy, and approval log record.
- Ship three verified prototype skills: OMOP cohort summary with QC checks, aggregate export with the 20-participant floor, and a baseline model over synthetic data.
- Add unit tests for each skill's scripts, schema, metadata, and export guards.
- Add one replay fixture for the full synthetic workflow, including expected tool calls, policy decisions, audit events, aggregate output, and attestation.

**Completion criteria:** local skill directories verify through a root-confined gate before activation; verified skills require signed-provenance placeholders, no runtime network requirement, declared tool lists, and approval copy; the three prototype skills run deterministically against synthetic OMOP-like fixtures; aggregate export tests prove sub-floor counts are suppressed; replay fixture validation ties the synthetic workflow to expected tool calls, policy decisions, audit events, outputs, and attestations; and the standard repository checks remain green.

### 0E — CLI, notebook bridge, image, and reviewer packet

- Build one self-contained Docker image for the generic Linux/Jupyter path.
- Provide CLI commands that run the full synthetic workflow without external network access: `heartwood detect`, `heartwood chat`, `heartwood run`, `heartwood replay`, and `heartwood audit export`.
- Make `heartwood chat` an agent-like terminal session with chat turns, visible tool/code events, approve/deny prompts, pause/resume, and replay.
- Provide a notebook API and minimal `ipywidgets` bridge that can attach to the same session, display chat/activity/proposed approvals, and trigger the same commands.
- Generate a scrubbed audit bundle and minimal egress-attestation report from the synthetic workflow.
- Include a reviewer packet: threat model summary, data-flow diagram, policy profile, fixture statement, sample audit log, sample attestation, and current limitations.

**Exit:** a user can build or pull the generic image, run the synthetic workflow from the CLI, accept the detected OMOP-like skill proposal, complete cohort → QC → baseline model, and export aggregate results plus an attestation. The notebook bridge can attach to the same session events without changing execution semantics. Required checks pass: repository validation, Python quality, unit tests, no-live-data fixture checks, synthetic replay, coverage, link checks, and container smoke test.

## Extension and testability mechanisms

- **Adapter contract tests:** each platform, model, data, and registry adapter ships conformance fixtures and must pass the shared adapter test suite.
- **Dependency-injection boundary:** core packages depend on protocols and typed settings, so tests can replace platform, data, model, registry, storage, clock, and network services with deterministic fakes.
- **Schema compatibility tests:** policy, audit, skill metadata, and attestation schemas are versioned; breaking changes require migration tests.
- **Fake providers:** deterministic platform/data/model adapters are first-class test tools, not ad hoc mocks hidden in individual tests.
- **Replay boundary:** all externally visible decisions are emitted as structured events, allowing tests to replay sessions without a live model or platform.
- **Interface contract tests:** CLI outputs and notebook view models are derived from the same session events and covered by snapshot tests.
- **Golden audit artifacts:** replay tests compare scrubbed audit logs and attestations against checked-in expected outputs.
- **Network-deny tests:** policy tests exercise allowed endpoint matching, blocked public egress, and denied runtime dependency installation.
- **Skill test harness:** every skill includes metadata validation, script tests, declared-tool checks, export-guard tests, and at least one replay or golden output.
- **No-live-data checks:** fixture scans block secrets, direct identifiers, and PHI-shaped values before merge.

## Repository strategy

### Core repository

This repository remains the system of record through Phase 1. It contains:

- core Python packages, schemas, and CLI;
- notebook API and widget presentation adapter;
- generic adapter implementations and adapter test harnesses;
- prototype verified skills;
- synthetic fixtures and replay tests;
- compliance-kit templates and generated sample artifacts;
- Dockerfiles and Docker Compose smoke tests;
- CI workflow callers and repository governance files.

### Near-future repositories and external elements

- **GHCR packages:** publish signed generic and platform-specific images from this repository once the generic image passes smoke tests.
- **Skill catalog repository:** create a separate `heartwood-skills` repository only after the local skill metadata schema, trust tiers, and skill eval harness are stable.
- **Platform adapter repositories:** split adapters only when a platform requires a different release cadence, private validation fixtures, or platform-specific maintainers.
- **Evaluation repository:** split large replay suites or benchmark data only when they become too large or too sensitive for the core repo.
- **Documentation site:** keep design docs in the core repo initially; publish a separate site only when external user documentation becomes release-managed.

No separate marketplace, registry service, or platform-specific repo is needed for Phase 0. Those would add coordination overhead before the contracts are stable.

## Phase 1 — First reference platform and real-data validation

- Select one primary pilot platform and one secondary adapter target.
- Implement the primary platform adapter, model endpoint wiring, credential allowlist, platform-specific image base, policy profile, and platform note.
- Build an air-gapped image variant with vendored dependencies and verified skills; verify signatures at build and load.
- Expand the compliance kit with platform-specific language and validate it against a real institutional review.
- Validate the Phase-0 workflow on controlled data only after the synthetic replay suite, reviewer packet, and primary-platform policy profile pass review.

## Phase 2 — Skill breadth and second data type

- Add the community skill tier, signing flow, approval UX, scheduled scans, and skill eval gates.
- Aggregate external skill registries through a verified import process.
- Add one second data adapter, either genomics/VCF or FHIR, to prove detector and adapter generality.
- Add a second compliant in-boundary model path to exercise model-provider portability.

## Phase 3 — Scale-out and improvement loop

- Emit Dockstore-registered CWL/WDL/Nextflow workflows for batch scale.
- Operationalize the export → validate → improve loop across consenting sites.
- Add local-model support as an explicit supervised tier.
- Add additional platform adapters based on demand and maintenance capacity.

## Repo layout

```
/README.md  /ACRONYMS.md  /AGENTS.md  /CONTRIBUTORS.md  /LICENSE  /LICENSES/
/.github                    # CODEOWNERS, workflow callers, codecov, dependabot
/design                     # this documentation set
/pyproject.toml  /uv.lock  /.pre-commit-config.yaml   # Python workspace + local hooks
/packages
  /core-adapter             # session orchestration and agent backend facade
  /adapters                 # adapter SPI protocols, conformance checks, and generic/local adapters
  /schemas                  # versioned policy, audit, detection, skill, and approval schemas
  /session                  # command/event API shared by all interfaces
  /fixtures                 # synthetic fixture linting and no-live-data checks
  /model-policy             # policy profiles, capability tiers, attestation
  /detector                 # environment/dataset detection
  /skills                   # verification gate + heartwood.* metadata semantics
  /audit                    # hash-chained log + scrubbed export
  /adapters/{platform,model,data,registry}/*   # concrete adapter implementations
  /mcp-servers              # data gateway, omop, fhir, drs, notebook
  /cli                      # primary interaction surface
  /notebook                 # Python API + ipywidgets presentation adapter
/skills                     # SKILL.md dirs (verified/ community/ experimental/)
/compliance                 # templates and generated reviewer packets
/images                     # generic and platform Dockerfiles
/evals                      # replay suites, skill evals, benchmark harness
/fixtures                   # synthetic data only
```

## First implementation backlog

This is the linear ordering of the 0A–0E work; each item is a small, reviewable pull request.

1. Bootstrap repository health files and CI callers.
2. Add the Python workspace skeleton, locked dependencies, lint/type/test config, and a passing empty test suite.
3. Define the session command/event contract.
4. Define adapter, policy, skill metadata, audit, and attestation schemas.
5. Add synthetic OMOP-like fixtures and no-live-data fixture checks.
6. Implement generic/local adapters and adapter conformance tests.
7. Implement deny-egress policy tests and fake model-provider tests.
8. Implement deterministic session orchestration, the offline agent facade, local state persistence, hash-chained audit logging, and scrubbed audit export.
9. Add local `SKILL.md` verification and the skill test harness.
10. Build the interactive CLI over the session contract.
11. Add the notebook API and minimal widget bridge over the same contract.
12. Package the generic Docker image and run a Docker Compose smoke test in CI.
13. Add the three prototype skills and the end-to-end synthetic replay.
14. Produce the reviewer packet.
15. Select the first real platform pilot.

## Open questions

- Which single platform is the first real-data pilot.
- Which group owns clinical/statistical review for the `verified` skill tier.
- What long-term funding and governance model supports maintenance.
- How rich the Phase-0 notebook widget bridge should be beyond the shared session contract.
- The final project name.

## Known limitations

Agentic-coding quality degrades on weak/local models (mitigated by capability tiers and the supervised path); the sandbox network proxy is TLS-blind (mitigated by platform egress-deny); dataset fingerprinting can mis-detect unusual schemas (mitigated by propose-not-commit).
