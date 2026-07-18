<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Engineering and Releases

## Languages and Toolchain

- **Python 3.12 runtime.** The core, gateway, OpenHands adapter, model settings, detector, policy, audit, CLI, notebook bridge, and image scripts use the locked `uv` workspace. Ruff, strict mypy, pytest with branch coverage, and Pydantic are the implemented quality tools.
- **TypeScript web UI.** The Stanford Spezi web stack, React, Vite, strict TypeScript, ESLint, Prettier, Vitest, Testing Library, and Playwright implement and verify the conversation interface.
- **Shell and Docker.** Shell scripts remain small orchestration entrypoints checked with syntax tests. Docker BuildKit and Bake define the generic and platform-derived images.
- A new language, service, or frontend stack requires a documented need that cannot be met through the existing code or an upstream OpenHands capability.

## Repository Hygiene

Use short-lived branches, reviewed pull requests, locked dependencies, synthetic fixtures, SPDX headers, and narrowly scoped changes. Organization community-health files and reusable validation workflows remain the canonical contribution layer. One dependency-based `Main Validation` workflow orchestrates reusable component workflows on pull requests and `main`; its final release-readiness job succeeds only when every event-appropriate component succeeds. A superseding run cancels an in-progress run for the same pull request or non-`main` branch, while every `main` run completes independently. Component workflows remain manually dispatchable for diagnostics. Required checks block merges and releases rather than live sessions.

Dependencies are admitted only when they provide a maintained capability that is impractical to implement safely in the existing stack. Agent behavior must use the pinned OpenHands SDK and tools; provider compatibility must use LiteLLM; Hugging Face repository inspection and local artifact retrieval must use `huggingface_hub`; notebook routing must use Jupyter infrastructure; and container publication must use standard Docker and OCI tooling. Dependency-specific behavior belongs behind one adapter with conformance tests so upgrades do not spread version assumptions through the product.

## Pre-1.0 Change Policy

Before `1.0.0`, Heartwood optimizes for a small, internally consistent product rather than compatibility with unreleased interfaces. A replacement removes obsolete command forms, environment-variable configuration, state layouts, schemas, and internal APIs instead of retaining aliases, dual readers, or parallel execution paths. The same change must update every interface, platform adapter, test, and current operational document that uses the replaced contract.

A breaking change must still fail clearly and protect user data. Heartwood must not silently reinterpret incompatible state, overwrite an existing project, expose credentials, or claim that an old layout was migrated when no tested migration exists. Release notes identify user-visible breaks and the supported clean setup. Compatibility or migration code requires a concrete data-integrity, security, or deployment need recorded in the owning issue; anticipated post-`1.0.0` compatibility is not sufficient reason to carry it now.

## Implemented Continuous Integration

- **Validate:** REUSE, actionlint, Markdown links, yamllint, and whitespace.
- **Documentation:** canonical-source staging and a strict Zensical build on pull requests and `main`; a local publication smoke test verifies version paths and stable and preview aliases without changing the public site.
- **Python:** locked install, Ruff format and lint, strict mypy, synthetic fixture lint, pytest, enforced branch coverage, and Codecov upload.
- **Web UI:** locked npm install, Prettier, ESLint, strict TypeScript, Vitest coverage, asset build, npm license inventory, npm audit, Playwright, packaged-gateway smoke, and Jupyter-proxy smoke.
- **Security:** CodeQL, Gitleaks, Dependabot, and public-repository dependency review.
- **Containers:** native AMD64 and ARM64 no-weight builds, BuildKit checks, offline OpenHands allow/reject integration, a separately mounted llama.cpp fixture, Terra Jupyter contracts, cache-only pull-request builds that run the shared GPU dependency and no-weight assertions without serializing complete local images, and publication manifest validation.
- **Publication:** generic and Terra builds stage untagged content-addressed manifests, validate the exact staged digests, create and verify immutable commit tags, refuse to overwrite a commit tag with different descriptors, and move `edge` or `edge-terra` only as the final promotion step. Generic architecture digests are merged with software bill of materials and provenance attestations; Terra preserves a Docker schema-2 AMD64 manifest without attestations because Leonardo rejects the generic Open Container Initiative index shape.
- **Release governance:** a manually dispatched workflow accepts strict Semantic Versioning without a prefix, requires matching source-package versions, binds the candidate to the current `main` commit, requires the reviewed main-check manifest, verifies every immutable CPU, Terra, and GPU image, and rebuilds and tests native assets. It creates an attested draft with GitHub-generated notes before pausing at the protected `release` environment so the maintainer can refine prose without changing artifacts. Only the approved publication job creates immutable versioned image tags and publishes the Git tag and GitHub Release; the subsequent Pages job checks out that published tag, adds its canonical documentation to a version-specific path, moves only the matching stable or preview alias, and deploys the complete version store through the `github-pages` environment.

The workflows use least-privilege permissions and do not expose repository secrets to pull-request jobs. Package publication uses `GITHUB_TOKEN` only in the main publication workflow.

## Licensing and Attribution

The repository is MIT licensed and REUSE compliant. Every tracked source file carries SPDX metadata, and the web workflow checks npm package licenses. GitHub dependency review adds license and vulnerability context when the repository is public. Python dependency inventory and generated third-party notices are not published.

## Testing and Documentation

Pure-code safety paths have enforced coverage, while CLI, notebook, REST, transport, web, and image behavior use contract and integration tests described in [Testing and Evaluation](07-testing-eval.md). All public fixtures and traces are synthetic. The [documentation index](../docs/README.md) defines document roles and status vocabulary: `docs/` owns current operational guidance and platform evidence, the numbered `design/` documents own durable rationale, and [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) with the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) own planned implementation and delivery status. Documentation checks must reject broken internal links, obsolete image tags, unsupported-platform claims, version-relative implementation narrative, and planned work presented as implemented behavior.

## Supply Chain

Application dependencies are hash-locked, image base versions and downloaded runtime archives are pinned, and recommended model artifacts require immutable revisions, byte sizes, SHA-256 digests or exact snapshot manifests, and license metadata. User-selected Hugging Face repositories are resolved to immutable revisions before transfer and retain the inspected provenance and resource plan in project state. Published images contain no credentials or model weights. BuildKit attestations are implemented for the generic image. Failed staging digests remain untagged and cannot move a public channel. Graph-aware GHCR retention, cryptographic signing, generated notices, and formal support governance are not implemented.

Native installation assets are built from the exact release candidate by GitHub Actions. Pull requests and every `main` commit build the same bundle and exercise verification and dry-run installation without publishing. The protected release workflow rebuilds the bundle with the approved Semantic Version, exercises the installation smoke test, attests the files, and attaches the fixed-name source bundle, standalone installer, and SHA-256 manifest to the release. The installer rejects a missing or mismatched manifest and supports an explicitly supplied local bundle for restricted-network transfer workflows.
