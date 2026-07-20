<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Development Guide

Heartwood is a Python workspace with a TypeScript researcher web interface, container and native packaging, and repository-level compliance and release tests.

## Set Up the Repository

```bash
git clone https://github.com/SchmiedmayerLab/heartwood.git
cd heartwood
uv sync --locked --all-groups
npm ci --prefix packages/webui
```

Use the repository's pinned Python and Node dependency locks.
Do not add a new language, agent implementation, UI stack, or service when an existing contract or upstream dependency can own the behavior.

## Reuse Before Variants

The session gateway owns setup choices, researcher-facing model metadata, readiness, action settings, and session behavior.
The terminal, browser, and notebook packages adapt those shared projections to their interface; they must not maintain parallel business rules or persisted settings.

Generic and platform-derived artifacts use `images/Dockerfile` and `docker-bake.hcl` as one assembly path.
Native and container GPU variants use `images/gpu/install_runtime.sh` for the same pinned vLLM environment.
Add a parameter, platform adapter, or validation target for a real platform difference instead of copying installation or packaging steps.

## Package Ownership

| Area | Package or Directory |
|---|---|
| Typed schemas and session commands/events | `packages/schemas`, `packages/session` |
| Platform and data adapters | `packages/adapters` |
| Policy and audit | `packages/model-policy`, `packages/audit` |
| OpenHands and core session orchestration | `packages/core-adapter`, `packages/gateway` |
| Terminal interface and runtime launch | `packages/cli` |
| Notebook bridge | `packages/notebook` |
| Browser interface | `packages/webui` |
| Skills and synthetic fixtures | `skills`, `fixtures/synthetic` |
| Images, native packaging, and release logic | `images`, `deploy`, `.github/workflows` |
| Public documentation | `documentation` |

## Run Checks

```bash
uv run ruff check .
uv run mypy
uv run pytest
npm run --prefix packages/webui lint
npm run --prefix packages/webui typecheck
npm test --prefix packages/webui
npm run --prefix packages/webui build
uv run zensical build --clean --strict
```

Run focused tests while iterating, then run the complete affected suites before review.
Container and capable-model checks have higher resource requirements and run through their documented workflows.

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
Artifact tests should verify that all variants resolve through the canonical assembly path as well as testing each supported runtime environment.

Keep planned work and acceptance criteria in GitHub Issues rather than public documentation.
