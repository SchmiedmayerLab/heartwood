<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Development Guide

Heartwood is a Python workspace with a TypeScript researcher web interface built on [Grove](https://github.com/SchmiedmayerLab/grove-ts), container and native packaging, and repository-level compliance and release tests.

## Set Up the Repository

```bash
git clone --recurse-submodules https://github.com/SchmiedmayerLab/heartwood.git
cd heartwood
uv sync --locked --all-groups --all-extras
npm ci --prefix packages/webui
```

Use the repository's pinned Python and Node dependency locks.
Resolution ignores releases younger than seven days through uv's `exclude-newer` setting, npm's `min-release-age`, and the Dependabot cooldown; security updates are not delayed.
Base images are pinned by digest, and third-party GitHub Actions use exact versions from immutable releases, with Dependabot proposing updates in the weekly group.
Do not add a new language, agent implementation, UI stack, or service when an existing contract or upstream dependency can own the behavior.

## Reuse Before Variants

The session gateway owns setup choices, researcher-facing model metadata, readiness, action settings, and session behavior.
The terminal, browser, and notebook packages adapt those shared projections to their interface; they must not maintain parallel business rules or persisted settings.
The browser consumes Grove's design-system components and shared TypeScript configuration rather than maintaining project-local equivalents.

Generic and platform-derived artifacts use `images/Dockerfile` and `docker-bake.hcl` as one assembly path.
Native and container GPU variants use `images/gpu/install_runtime.sh` for the same pinned vLLM environment.
Add a parameter, platform adapter, or validation target for a real platform difference instead of copying installation or packaging steps.

### Worker Image

`docker buildx bake worker` builds the native agent server through the same image stages and workspace lock.
The optional `worker` dependency group pins the server to the SDK and tool version; it is not installed in the ordinary application image.
Use `uv sync --locked --all-groups --all-extras` to include its configuration checks locally.
Run `python images/worker/smoke.py IMAGE` against the locally loaded worker image to verify native command execution, authentication, private workspace persistence, read-only configuration, and removal of synthetic inherited secrets.
The check creates a disposable container with no network or host mounts and removes it afterward.
The worker target is a Linux CPU qualification artifact and is not connected to the gateway's local conversation factory.

The entry point requires a fresh URL-safe `SESSION_API_KEY` with at least 43 characters and accepts only the optional `HEARTWOOD_INFERENCE_PLACEHOLDER` from the launching environment.
Generate the key with a cryptographically secure random source for each attempt, never reuse it between workers, and deliver it through the sandbox's secret mechanism.
The placeholder must be issued by the outside inference transport; never supply an actual provider credential under that name.
All remaining environment values are fixed in the image: system paths and locale, private worker home, disabled telemetry, and the read-only server configuration.
Proxy settings, certificate overrides, dynamic loader settings, Python import paths, arbitrary server overrides, host identity, and other credentials are not forwarded.
The entry point closes inherited descriptors other than standard input, output, and error before replacing itself with the native server.
It binds to sandbox loopback, disables browser and editor services, and admits one concurrent native conversation run.

Launch this target through a qualified sandbox with no host environment inheritance, scoped volumes, non-root identity, resource limits, and outside network enforcement.
The initial executable must already be launched under that boundary: cleaning its environment cannot undo a malicious dynamic-loader setting used before the bootstrap runs.
Worker conversation and command state is private, untrusted runtime data under the worker home; it is never canonical gateway state and can contain sensitive content.
Its retention and deletion require deployment controls.
An image, a loopback bind, and environment filtering alone do not establish isolation, independent approval enforcement, or sensitive-data readiness.

## Package Ownership

| Area | Package or Directory |
|---|---|
| Typed schemas and session commands/events | `packages/schemas`, `packages/session` |
| Platform and data adapters | `packages/adapters` |
| Policy and audit | `packages/model-policy`, `packages/audit` |
| Durable files and persisted-schema migrations | `packages/persistence` |
| OpenHands and core session orchestration | `packages/core-adapter`, `packages/gateway` |
| Terminal interface and runtime launch | `packages/cli` |
| Notebook bridge | `packages/notebook` |
| Browser interface | `packages/webui` |
| Skill acquisition and project activation | `packages/skills`, `packages/gateway` |
| Pinned curated Skill source and shared catalog tooling | `vendor/heartwood-skills` from [`SchmiedmayerLab/heartwood-skills`](https://github.com/SchmiedmayerLab/heartwood-skills) |
| Synthetic fixtures | `fixtures/synthetic`, `evals` |
| Images, native packaging, and release logic | `images`, `deploy`, `.github/workflows` |
| Public documentation | `documentation` |

## Run Checks

```bash
uv run ruff check .
uv run vulture
uv run mypy packages
uv run pytest
npm run --prefix packages/webui lint
npm run --prefix packages/webui duplicates:check
npm run --prefix packages/webui contracts:check
npm run --prefix packages/webui typecheck
npm test --prefix packages/webui
npm run --prefix packages/webui build
uv run --group docs zensical build --clean --strict
```

Run focused tests while iterating, then run the complete affected suites before review.
Container and capable-model checks have higher resource requirements and run through their documented workflows.

The curated Skill source must be initialized at the exact Git revision recorded by the superproject.
CI rejects missing, modified, or substituted submodule content.
Changes to curated Skill packages and catalog tooling are reviewed in `heartwood-skills`; Heartwood changes then advance the pinned revision and update interface, storage, and release tests together.

## Qualify a Terra Image Before Merge

Ordinary pull-request validation builds the GPU targets without exporting an image.
When a change requires live Terra qualification, push the reviewed commit and manually run **Terra GPU Qualification Candidate** for that branch.

The workflow publishes one commit-bound `candidate-sha-…-terra-gpu-nvidia` tag, verifies its Docker media type and embedded revision, and runs the shared Terra image smoke tests.
Use the tag reported in the workflow summary when recreating the Terra environment.
The candidate does not move release or `edge` tags.

## CI Runners and Runtime Locks

Standard GitHub-hosted runners run every job except capable-model acceptance: `ubuntu-24.04` for x64 jobs and `ubuntu-24.04-arm` for ARM images.
Container builds start with the shared `reclaim-runner-disk` action, which removes preinstalled toolchains the job does not use and reports the remaining space.
The Terra image jobs carry longer timeouts because the Terra base image is large.
Capable-model acceptance runs on the organization's `heartwood-ubuntu-large` hosted runner because llama.cpp prompt processing on a standard runner does not finish inside the qualification command deadline.
The job verifies at least 30 GiB of memory so it cannot silently run on a standard runner, and it matches llama.cpp threads to the runner's cores.
The runner group is limited to this repository, and fork pull requests need approval before their workflows run; keep the large-runner label out of every other workflow.

Regenerate the isolated GPU dependency lock with `bash images/gpu/compile_requirements.sh`.
This is the same command CI uses to verify the lock, including its release-date cutoff and hashes.
Update the security floors in `images/gpu/vllm.in` and the cutoff in that script when refreshing dependencies; do not edit generated hashes by hand.

## Static Analysis

Python packages use strict mypy checking with the Pydantic plugin, and each published namespace subpackage includes a PEP 561 `py.typed` marker.
Public REST requests use strict Pydantic models, while gateway responses use exact typed mappings that are validated before they cross an interface boundary.

The browser API types are generated from those Python contracts.
After changing a shared request or response, run:

```bash
npm run --prefix packages/webui contracts:generate
```

CI fails if the generated file is stale.
Browser-only presentation models remain in TypeScript and must not duplicate gateway payload definitions.

Ruff identifies unused imports, variables, arguments, and commented-out code.
CI also runs Vulture at 100 percent confidence to detect unreachable or unused definitions beyond Ruff's local checks.
Lower-confidence Vulture findings remain a review aid because framework callbacks and implicit fixtures can appear unused:

```bash
uv run vulture packages --min-confidence 80
```

The production duplication check uses a conservative threshold to detect growth without forcing unrelated platform adapters or interface forwarding into artificial abstractions.
Review every reported clone and extract it only when the behavior has one clear owner.

## Change a Shared Contract

When changing project state, startup, models, actions, sessions, Skills, or audit behavior:

1. update the owning typed contract;
2. update gateway behavior;
3. update terminal, browser, and notebook projections;
4. update platform behavior where capabilities differ;
5. add regression and integration coverage;
6. update current documentation and reference pages; and
7. remove the superseded pre-1.0 path instead of maintaining an undocumented compatibility layer.

Tests for a shared contract should compare the gateway, REST, terminal, browser, and notebook projections that expose it.
Persisted-format changes require checked-in compatibility fixtures that pass both the shared migration registry and the owning domain loader.
Durability changes require interruption tests at every write boundary, process-level concurrency tests, and explicit recovery and corruption cases.
Artifact tests should verify that all variants resolve through the canonical assembly path as well as testing each supported runtime environment.

Keep planned work and acceptance criteria in GitHub Issues rather than public documentation.
