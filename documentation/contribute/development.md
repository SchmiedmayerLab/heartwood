<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Development Guide

Heartwood is a Python workspace with a TypeScript researcher web interface built on [Grove](https://github.com/SchmiedmayerLab/grove-ts), container and native packaging, and repository-level compliance and release tests.

## Set Up the Repository

The bootstrap requires uv 0.13.7 exactly.
If another uv is installed, select the pinned one for these commands:

```bash
git clone --recurse-submodules https://github.com/SchmiedmayerLab/heartwood.git
cd heartwood
UV=$(uvx --from uv==0.13.7 which uv)
$UV run --no-project --python 3.12.15 deploy/build-worker-candidate.py --bootstrap --uv "$UV"
$UV sync --locked --all-groups --all-extras
npm ci --prefix packages/webui
```

`--recurse-submodules` checks out the curated Skill source in `vendor/heartwood-skills`; in an existing clone run `git submodule update --init`.
The bootstrap builds the maintained OpenHands and OpenShell wheels into `vendor/runtime-wheels/`, which the workspace lock resolves from, after verifying upstream source and patch hashes, preserved licenses, wheel digests, and the build record.
A later bootstrap only verifies the existing wheels.
If verification fails, keep the files for inspection and start from a clean checkout; never bypass hashes or override constraints.
`--all-groups` adds the `dev`, `docs`, and `worker` dependency groups.
Installing Heartwood packages from a package registry without these wheels is not supported; the native release archive and container images build them through the same bootstrap.

Resolution ignores releases younger than seven days through uv's `exclude-newer`, npm's `min-release-age`, and the Dependabot cooldown; security updates are not delayed.
Base images are pinned by digest, and third-party GitHub Actions use exact versions from immutable releases.

## Reuse Before Variants

The session gateway owns setup choices, researcher-facing model metadata, readiness, action settings, and session behavior.
The terminal, browser, and notebook packages adapt those shared projections to their interface; they must not maintain parallel business rules or persisted settings.
The browser consumes Grove's design-system components and shared TypeScript configuration rather than maintaining project-local equivalents.

Generic and platform-derived artifacts use `images/Dockerfile` and `docker-bake.hcl` as one assembly path.
Native and container GPU variants use `images/gpu/install_runtime.sh` for the same pinned vLLM environment.
Add a parameter, platform adapter, or validation target for a real platform difference instead of copying installation or packaging steps.

### Worker Image

`docker buildx bake worker` builds the managed worker image, the OpenHands agent server from the `worker` dependency group, through the same image stages and workspace lock; the application image does not contain it.
`python images/worker/smoke.py IMAGE` checks a locally loaded worker image in a disposable container with no network or host mounts: native command execution, authentication, private workspace persistence, read-only configuration, and removal of synthetic inherited secrets.
Local launches run OpenHands in the gateway process; only a deployment manifest's `[worker]` table launches this image, through OpenShell.

The entry point, `images/worker/launch.py`, requires a fresh URL-safe `SESSION_API_KEY` of at least 43 characters and accepts only an optional `HEARTWOOD_INFERENCE_PLACEHOLDER` and OpenShell's supervisor trust files under `/run/openshell-supervisor-ca/material/` from the launching environment.
Every other environment value is fixed in the image, inherited descriptors beyond standard streams are closed, and the server binds to sandbox loopback with one concurrent conversation run.
These measures do not create isolation: the image must be started inside a qualified sandbox with no inherited host environment, scoped volumes, a non-root identity, resource limits, and outside network enforcement.
[Security and Controlled Data](../operate/security.md) describes the worker boundary and what it does not establish.

### Installed Gateway Resources

The gateway wheel includes the canonical model catalogs and manifests, reviewed specialist definitions and pinned bundled Skills through package build mappings.
The installed gateway loads these resources from its own package directory.
Editable source installations use the same repository resources; an installed package with missing resources fails instead of searching the research project or unrelated ancestor directories.
The CLI wheel also includes synthetic fixture data; the shared image assembly supplies its compiled browser assets.
A standalone CLI wheel without those browser assets requires an explicit web-root selection.
The candidate workflow tests the installed wheels with isolated Python imports, including CLI and subprocess behavior.

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
| Kubernetes reference platform, outside the application workspace | `reference-platform` |
| Public documentation | `documentation` |

## Run Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run vulture
uv run mypy packages
uv run heartwood-fixtures fixtures evals
uv run pytest
npm run --prefix packages/webui lint
npm run --prefix packages/webui duplicates:check
npm run --prefix packages/webui contracts:check
npm run --prefix packages/webui typecheck
npm test --prefix packages/webui
npm run --prefix packages/webui build
uv run --group docs zensical build --clean --strict
python3 reference-platform/tools/check_cloud_boundary.py
(cd reference-platform/services && uv sync --locked && uv run mypy && uv run pytest)
```

Run focused tests while iterating, then run the complete affected suites before review.
Container, capable-model, and native worker checks need more resources and run through their workflows; see [Testing and Evidence](../architecture/testing.md).

The curated Skill source must be initialized at the exact Git revision recorded by the superproject.
CI rejects missing, modified, or substituted submodule content.
Changes to curated Skill packages and catalog tooling are reviewed in `heartwood-skills`; Heartwood changes then advance the pinned revision, update `BUNDLED_SKILL_DIGESTS` in the gateway's Skill settings, and update interface, storage, and release tests together.
After advancing the revision, run `uv run pytest packages/gateway/tests/test_skill_settings.py -k pin_matches`; its failure shows the complete-tree digests to copy into `BUNDLED_SKILL_DIGESTS`.
The Python workflow runs the same test with the submodule checked out, so a stale pin fails continuous integration.

## Qualify a Terra Image Before Merge

Ordinary pull-request validation builds the GPU targets without exporting an image.
When a change requires live Terra qualification, push the reviewed commit and manually run **Terra GPU Qualification Candidate** for that branch.

The workflow publishes one commit-bound `candidate-sha-…-terra-gpu-nvidia` tag, verifies its Docker media type and embedded revision, and runs the shared Terra image smoke tests.
Use the tag reported in the workflow summary when recreating the Terra environment.
The candidate does not move release or `edge` tags.

## CI Runners and Runtime Locks

Pull-request and main validation run on standard GitHub-hosted runners (`ubuntu-24.04`, `ubuntu-24.04-arm` for ARM images, and `ubuntu-latest` for some checks), with two exceptions.
Capable-model acceptance runs on the organization's `heartwood-ubuntu-large` hosted runner, because llama.cpp prompt processing on a standard runner misses the qualification deadline; the job requires at least 30 GiB of memory and matches llama.cpp threads to the runner's cores.
That runner group is limited to this repository and fork pull requests need approval before their workflows run; keep its label out of every other workflow.
GPU coding-agent qualification is a manually started workflow on a self-hosted GPU runner.
GPU image builds start with the `reclaim-runner-disk` action, which removes unused preinstalled toolchains.

Hosted jobs differ from a local checkout in two ways that matter when a result differs.
The Python job installs with `uv sync --locked --all-extras` (default groups only) after the same bootstrap, and installed-wheel and native qualification jobs set `OPENHANDS_SUPPRESS_BANNER=1` and `LITELLM_LOCAL_MODEL_COST_MAP=True` so no model cost map is fetched from the network.
Native worker qualification needs Docker and builds the sandbox gateway; it runs for every main commit and for pull requests that change anything outside `documentation/` and `.github/`, or its own or the pull-request validation workflow.

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
