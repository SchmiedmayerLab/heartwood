<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Development and Release Engineering

This page records durable contributor contracts. Setup commands and review expectations are in [Contributing](../CONTRIBUTING.md).

## Toolchain

- Python 3.12, uv, Pydantic, Ruff, mypy, and pytest implement and verify the core, gateway, OpenHands adapter, policy, audit, CLI, notebook, and image tooling.
- TypeScript, React, Stanford Spezi Web, Vite, ESLint, Prettier, Vitest, Testing Library, and Playwright implement and verify the browser application.
- Docker BuildKit and Bake define generic and platform-derived images.
- Shell scripts remain bounded orchestration entrypoints.

## Dependency Policy

Use maintained upstream capabilities instead of reproducing them:

- OpenHands for agent execution, tools, confirmation, persistence, Skills, and context management;
- LiteLLM through OpenHands for provider compatibility;
- Hugging Face Hub for repository metadata and transfer;
- Textual for the terminal application;
- Jupyter for notebook and proxy integration; and
- standard Docker and Open Container Initiative tooling for images.

Dependency-specific behavior belongs behind one adapter with conformance tests.

## Pre-1.0 Changes

Before `1.0.0`, Heartwood prefers one coherent contract over compatibility with unreleased commands, flags, environment variables, state layouts, or APIs. A replacement removes obsolete paths and updates implementation, tests, documentation, and release notes together.

A breaking change must still protect user data and fail clearly. Heartwood must not silently reinterpret incompatible state, overwrite a project, or expose credentials.

## Repository Quality

Pull requests use locked dependencies, synthetic fixtures, SPDX metadata, strict Python and TypeScript checks, branch coverage, browser tests, security scanning, container validation, and documentation builds.

The main validation workflow composes component workflows and reports release readiness only after every event-appropriate dependency succeeds. Release workflows verify the exact main commit and immutable artifacts rather than rebuilding unreviewed source after approval.

## Documentation Ownership

- `README.md` is the repository overview.
- `documentation/index.md` is the public documentation home.
- `docs/` contains current user, operator, reference, and maintainer instructions.
- Numbered `design/` documents contain durable architecture and rationale.
- GitHub Issues and the Heartwood Project contain planned work, acceptance criteria, and delivery status.
- Pull requests and CI artifacts contain implementation discussion and run-specific evidence.

Public documentation must not become a development diary, test transcript, or backlog.

## Releases

Heartwood uses Semantic Versioning, version-locked source metadata, protected release approval, immutable GitHub releases, verified native assets, and promoted container descriptors. Stable and prerelease documentation is published from the exact release tag into separate versioned channels.

See [Release Heartwood](../docs/releases.md) for the maintainer procedure and [Testing and Evidence](07-testing-eval.md) for claim boundaries.
