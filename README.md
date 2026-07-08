<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

[![Python](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/python.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/python.yml)
[![Validate](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/validate.yml)
[![CodeQL](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/codeql.yml)
[![Secret Scan](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/gitleaks.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/gitleaks.yml)
[![Dependency Review](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/dependency-review.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/dependency-review.yml)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

A Docker-packaged coding harness for sensitive biomedical research data.

Heartwood is designed to run inside trusted research platforms, close to controlled data. It provides platform detection, policy checks, skill packaging, audit records, and a CLI-first workflow for reproducible analyses.

Participant-level data stays inside the platform boundary. Development and CI use synthetic fixtures only.


## Overview

Heartwood builds the biomedical, platform, policy, skills, and audit layer around a reusable execution core. The project uses a Python workspace, typed contracts, platform adapters, and a shared session command/event model for all user interfaces.

The current repository contains the Phase 0B foundation: repository health files, CI, the `uv` workspace, a deterministic platform detector, adapter protocols, versioned schemas, synthetic fixture checks, the shared session contract, and the `heartwood` command-line interface. The full implementation plan is tracked in [design/09-implementation-plan.md](design/09-implementation-plan.md).


## Usage

Install [`uv`](https://docs.astral.sh/uv/) and run the local commands from a repository checkout:

```bash
uv sync
uv run heartwood --version
uv run heartwood detect
```

The `detect` command inspects environment markers and prints a proposal. It does not access data and does not make model calls.


## Repository Structure

- [`design`](design) contains the project design record and implementation plan.
- [`fixtures`](fixtures) contains synthetic test and schema-validation fixtures only.
- [`packages/adapters`](packages/adapters) contains adapter protocols and conformance checks.
- [`packages/cli`](packages/cli) contains the `heartwood` command-line interface.
- [`packages/detector`](packages/detector) contains deterministic platform detection.
- [`packages/fixtures`](packages/fixtures) contains no-live-data fixture linting.
- [`packages/schemas`](packages/schemas) contains versioned policy, audit, detection, skill, and approval schemas.
- [`packages/session`](packages/session) contains the shared session command/event contract.


## Development

The main local checks mirror CI:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy packages
uv run pytest
uv run reuse lint
uv run heartwood-fixtures fixtures
```

Do not add PHI, credentials, live-platform identifiers, or non-synthetic records to tests, fixtures, examples, issues, or pull requests.

## Documentation

| Doc | Contents |
|-----|----------|
| [01 · Overview](design/01-overview.md) | What it is, personas, scope |
| [02 · Platforms](design/02-platforms.md) | Target environments, embedding, in-boundary models, data-use policy |
| [03 · Architecture](design/03-architecture.md) | Core, adapter SPI, model policy, data flow |
| [04 · Skills](design/04-skills.md) | `SKILL.md`, auto-detection, sharing, skill trust |
| [05 · Security & compliance](design/05-security-compliance.md) | In-boundary enforcement, PHI, compliance kit, governance |
| [06 · Observability & audit](design/06-observability-audit.md) | Audit trail, tamper-evidence, feedback loop |
| [07 · Testing & evaluation](design/07-testing-eval.md) | Record/replay, evals, capability gate |
| [08 · Development](design/08-development.md) | Languages, linting, licensing, CI |
| [09 · Implementation plan](design/09-implementation-plan.md) | Phased delivery, repo layout, open questions |

## Contributing

Contributions to this project are welcome. Please make sure to read the [contribution guide](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [Contributor Covenant Code of Conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first.

Because Heartwood runs next to controlled data, contributors must never include PHI, credentials, or live-platform identifiers in issues, pull requests, tests, or fixtures; all fixtures are synthetic. Project-direction changes update the relevant [`design/`](design) document first, and security- or compliance-relevant claims must be backed by tests, audit records, or a documented limitation.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for more information.

## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-light.png#gh-light-mode-only)
![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-dark.png#gh-dark-mode-only)
