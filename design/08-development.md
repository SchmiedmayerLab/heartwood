<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 08 — Development Practices

## Languages And Toolchain

- **Python 3.12 runtime.** The core, gateway, OpenHands adapter, model settings, detector, policy, audit, CLI, notebook bridge, and image scripts use the locked `uv` workspace. Ruff, strict mypy, pytest with branch coverage, and Pydantic are the implemented quality tools.
- **TypeScript web UI.** The Stanford Spezi web stack, React, Vite, strict TypeScript, ESLint, Prettier, Vitest, Testing Library, and Playwright implement and verify the conversation interface.
- **Shell and Docker.** Shell scripts remain small orchestration entrypoints checked with syntax tests. Docker BuildKit and Bake define the generic and platform-derived images.
- A new language, service, or frontend stack requires a documented need that cannot be met through the existing code or an upstream OpenHands capability.

## Repository Hygiene

Use short-lived branches, reviewed pull requests, locked dependencies, synthetic fixtures, SPDX headers, and narrowly scoped changes. Organization community-health files and reusable validation workflows remain the canonical contribution layer. One dependency-based `Main Validation` workflow orchestrates reusable component workflows on pull requests and `main`; its final release-readiness job succeeds only when every event-appropriate component succeeds. Component workflows remain manually dispatchable for diagnostics. Required checks block merges and releases rather than live sessions.

Dependencies are admitted only when they provide a maintained capability that is impractical to implement safely in the existing stack. Agent behavior must use the pinned OpenHands SDK and tools; provider compatibility must use LiteLLM; local artifact retrieval must use the Hugging Face client; notebook routing must use Jupyter infrastructure; and container publication must use standard Docker and OCI tooling. Dependency-specific behavior belongs behind one adapter with conformance tests so upgrades do not spread version assumptions through the product.

## Implemented Continuous Integration

- **Validate:** REUSE, actionlint, Markdown links, yamllint, and whitespace.
- **Python:** locked install, Ruff format and lint, strict mypy, synthetic fixture lint, pytest, enforced branch coverage, and Codecov upload.
- **Web UI:** locked npm install, Prettier, ESLint, strict TypeScript, Vitest coverage, asset build, npm license inventory, npm audit, Playwright, packaged-gateway smoke, and Jupyter-proxy smoke.
- **Security:** CodeQL, Gitleaks, Dependabot, and public-repository dependency review.
- **Containers:** native AMD64 and ARM64 no-weight builds, BuildKit checks, offline OpenHands allow/reject integration, a separately mounted llama.cpp fixture, Terra Jupyter contracts, and publication manifest validation.
- **Publication:** generic and Terra builds stage untagged content-addressed manifests, validate the exact staged digests, create and verify immutable commit tags, refuse to overwrite a commit tag with different descriptors, and move `edge` or `edge-terra` only as the final promotion step. Generic architecture digests are merged with software bill of materials and provenance attestations; Terra preserves a Docker schema-2 AMD64 manifest without attestations because Leonardo rejects the generic Open Container Initiative index shape.
- **Release governance:** a manually dispatched workflow accepts strict Semantic Versioning without a prefix, requires matching source-package versions, binds the candidate to the current `main` commit, requires the reviewed main-check manifest, verifies every immutable CPU, Terra, and GPU image, and rebuilds and tests native assets. It creates an attested draft with GitHub-generated notes before pausing at the protected `release` environment so the maintainer can refine prose without changing artifacts. Only the approved publication job creates immutable versioned image tags and publishes the Git tag and GitHub Release.

The workflows use least-privilege permissions and do not expose repository secrets to pull-request jobs. Package publication uses `GITHUB_TOKEN` only in the main publication workflow.

## Licensing And Attribution

The repository is MIT licensed and REUSE compliant. Every tracked source file carries SPDX metadata, and the web workflow checks npm package licenses. GitHub dependency review adds license and vulnerability context when the repository is public. A complete Python dependency license inventory and generated third-party notice remain release-readiness work.

## Testing And Documentation

Pure-code safety paths have enforced coverage, while CLI, notebook, REST, transport, web, and image behavior use contract and integration tests described in [07](07-testing-eval.md). All public fixtures and traces are synthetic. The [documentation index](../docs/README.md) defines document roles and status vocabulary: `docs/` owns current operational guidance and platform evidence, `design/01` through `design/08` own durable rationale, and [09](09-implementation-plan.md) owns delivery priorities and acceptance gates. Documentation checks must reject broken internal links, obsolete image tags, unsupported-platform claims, version-relative implementation narrative, and planned work presented as implemented behavior.

## Supply Chain

Application dependencies are hash-locked, image base versions and downloaded runtime archives are pinned, and optional model artifacts require immutable revisions, byte sizes, SHA-256 digests, and license metadata. Published images contain no credentials or model weights. BuildKit attestations are implemented for the generic image. Failed staging digests remain untagged and cannot move a public channel; graph-aware GHCR retention, cryptographic image signing, real Skill signature verification, generated notices, formal release channels, and signature-policy enforcement remain future release controls.

Native installation assets are built from the exact release candidate by GitHub Actions. Pull requests and every `main` commit build the same bundle and exercise verification and dry-run installation without publishing. The protected release workflow rebuilds the bundle with the approved Semantic Version, exercises the installation smoke test, attests the files, and attaches the fixed-name source bundle, standalone installer, and SHA-256 manifest to the release. The installer rejects a missing or mismatched manifest and supports an explicitly supplied local bundle for restricted-network transfer workflows.
