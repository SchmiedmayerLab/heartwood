<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 08 — Development practices

Standards for building the tool itself.

## Languages and toolchain

- **Python** — core, MCP servers, detector, adapters, CLI, notebook API, and notebook widgets. `uv` (locked deps), `ruff` (lint + format), `mypy`/`pyright` (strict types), `pytest`, `pydantic` (typed schemas for metadata, policy, audit).
- **TypeScript** — deferred until a custom JupyterLab extension or standalone web UI is needed. Use `npm` scripts and `package-lock.json` when consuming the shared lab workflows; otherwise add an equivalent repo-local workflow. ESLint + Prettier, `tsc` strict, `vitest`.
- A compiled language is allowed later only for an isolated, perf-critical component.
- `pre-commit` hooks mirror CI so failures surface locally first.

## Licensing and attribution

- **REUSE compliance** — every file carries an SPDX license + copyright header; all license texts live in `LICENSES/`; the REUSE check runs in CI.
- **First-party license: MIT.**
- **Third-party attribution** — a generated `NOTICE`; a CI **license-compatibility gate** fails on any dependency incompatible with MIT redistribution, which also enforces "build on nothing proprietary."
- **SBOM** (CycloneDX/SPDX) generated per release and shipped with the image.

## Repository hygiene

Conventional Commits + semantic PR titles + generated changelog; `CODEOWNERS` plus the community health files — code of conduct, contributing guide, security policy, and issue/PR templates — inherited from `SchmiedmayerLab/.github`; trunk-based with short-lived branches and required review. Protect the default branch with a GitHub ruleset: required status checks, CODEOWNERS review for owned paths, linear history, and no direct pushes.

## CI/CD

CI uses reusable workflows in `SchmiedmayerLab/.github` where they fit and adds repo-local workflows only for gaps:

- **Repository validation:** create a repo-local `validate.yml` orchestrator that mirrors the lab pattern: REUSE, actionlint, Markdown links, yamllint, and whitespace. Call the reusable shared workflows for REUSE, actionlint, and Markdown links; run yamllint and whitespace locally.
- **Python:** repo-local workflow using `uv sync --locked`, `ruff format --check`, `ruff check`, strict `mypy`/`pyright`, `pytest`, and coverage upload.
- **TypeScript:** no Phase-0 requirement. Add the shared `eslint.yml` and `npm-test-coverage.yml` only when a package uses npm-compatible scripts (`lint:ci`, `test`); otherwise provide the same checks locally.
- **Containers:** shared `docker-compose-test.yml` for smoke tests; shared `docker-build-and-push.yml` for multi-architecture GHCR images after the prototype image is useful enough to publish.
- **Release and supply chain:** generated release notes, Codecov status checks, Dependabot for every manifest ecosystem, SBOM generation, Sigstore signing, and provenance attestations.
- **Workflow hardening:** least-privilege workflow permissions, no repository secrets in pull-request workflows, OIDC only for publishing, isolated fork runs, secret scanning, and dependency review before image publication.

Required branch checks for the first implementation: repository validation, Python quality/test/coverage, synthetic replay tests, container smoke test, and link checks. Gates block merges/releases, not live sessions.

## Testing and docs

High, enforced coverage on the pure-code safety-critical paths (detector, policy, audit, adapters), plus snapshot/contract tests for CLI commands and notebook view state; agentic behavior is covered by [07](07-testing-eval.md). All fixtures are synthetic. Docstrings + generated API docs per package; the `design/` set is the living architecture record; each adapter ships a short "how this platform is bridged" note.

## Supply-chain

Pinned, hash-locked dependencies; no floating ranges; no runtime installs. Sigstore signing + SLSA provenance for released images and skills, verified at build and load. The image is fully self-contained and reproducible where feasible.
