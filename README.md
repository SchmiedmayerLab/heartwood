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

Heartwood is designed to run inside trusted research platforms, close to controlled data. It provides platform detection, policy checks, skill packaging, audit records, and a CLI-first workflow for reproducible analyses, built on one session command/event contract that drives current CLI and notebook surfaces and planned researcher-web-UI surfaces.

Participant-level data stays inside the platform boundary. Development and CI use synthetic fixtures only.


## Overview

Heartwood builds the biomedical, platform, policy, skills, and audit layer around a reusable execution core. The architecture centers on a session gateway that owns the local agent-server boundary and exposes one shared session command/event contract to shipped surfaces such as the CLI and notebook bridge, and to planned surfaces such as the researcher web UI (see [design/03-architecture.md](design/03-architecture.md)). The project uses a Python workspace, typed contracts, platform adapters, verified local skills, and deterministic offline harnesses for local development and CI.

The current repository contains the core foundation: repository health files, CI, the `uv` workspace, deterministic platform detection, adapter protocols and generic/local adapters, versioned schemas, synthetic fixture checks, deny-by-default model policy, hash-chained audit logging, resumable session orchestration, local skill verification, a bundle catalog for packaged skills, prototype verified skills, replay fixtures, the session gateway package, the expanded `heartwood` command-line interface, a notebook bridge, synthetic evidence-bundle generation, generic image smoke-test configuration, and local-runtime profile metadata. The full implementation plan is tracked in [design/09-implementation-plan.md](design/09-implementation-plan.md).


## Usage

Install [`uv`](https://docs.astral.sh/uv/) and run the local commands from a repository checkout:

```bash
uv sync
uv run heartwood --version
uv run heartwood detect
uv run heartwood chat --prompt "summarize the synthetic cohort"
uv run heartwood run --endpoint https://model.local.invalid/v1/chat
uv run heartwood audit export
uv run heartwood reviewer packet
```

The `detect` command inspects environment markers, fingerprints the local synthetic fixture by filenames and headers only, and prints a proposal. The `chat`, `run`, `replay`, `audit export`, and reviewer-packet commands use the same session command/event contract as the notebook bridge.

Run the generic offline stack from Docker only after the main-branch image is published. The image is published for `linux/amd64` and `linux/arm64` where the dependency stack supports both platforms:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:dev-main
docker run --rm --network none ghcr.io/schmiedmayerlab/heartwood:dev-main bash images/generic/scripts/offline_stack_smoke.sh
```

The current generic image does not bundle an LLM inference runtime, model weights, or a production OpenHands agent-server. Its `stub-loopback` profile exists to prove the air-gapped session, policy, approval, audit, evidence-bundle, and local-endpoint plumbing; its agent-server coverage exercises the gateway-owned localhost boundary and fake OpenHands-style event translation. The selected real local-runtime profile is `llama-cpp-cpu`, which still needs the pinned runtime dependency, model artifact provenance, license review, checksum verification, resource validation, a pinned agent-server command, and an offline CLI-gateway-agent-server smoke test before the phase can close. Optional GPU acceleration is tracked as a separate profile because it depends on host GPU runtime support and GPU-specific image/runtime choices.

From a checkout, run the same CI smoke path with Compose:

```bash
docker compose -f images/generic/compose.yaml run --rm heartwood
```

See [Getting Started With The Offline Stack](docs/getting-started-offline.md) for the full walkthrough and current limitations.


## Repository Structure

- [`design`](design) contains the project design record and implementation plan.
- [`docs`](docs) contains tutorial-style project documentation.
- [`evals`](evals) contains synthetic replay fixtures.
- [`fixtures`](fixtures) contains synthetic test and schema-validation fixtures only.
- [`packages/adapters`](packages/adapters) contains adapter protocols, conformance checks, and generic/local adapter implementations.
- [`packages/audit`](packages/audit) contains hash-chained audit logging and scrubbed export support.
- [`packages/cli`](packages/cli) contains the `heartwood` command-line interface.
- [`packages/compliance`](packages/compliance) contains synthetic reviewer packet and audit bundle generation.
- [`packages/core-adapter`](packages/core-adapter) contains session orchestration and the deterministic offline agent facade.
- [`packages/detector`](packages/detector) contains deterministic platform detection.
- [`packages/fixtures`](packages/fixtures) contains no-live-data fixture linting.
- [`packages/gateway`](packages/gateway) contains ASGI HTTP command handling, replayable WebSocket event streams, managed local agent-server binding, and model egress gating.
- [`packages/model-policy`](packages/model-policy) contains deny-by-default model-call policy evaluation and attestation records.
- [`packages/notebook`](packages/notebook) contains the notebook Python API and optional widget bridge.
- [`packages/schemas`](packages/schemas) contains versioned policy, audit, detection, skill, and approval schemas.
- [`packages/session`](packages/session) contains the shared session command/event contract.
- [`packages/skills`](packages/skills) contains local `SKILL.md` verification, the package-time skill bundle catalog loader, and deterministic skill test helpers.
- [`images`](images) contains the generic image and Compose smoke-test configuration.
- [`skills`](skills) contains the checked-in skill bundle catalog and verified prototype skills.


## Development

The main local checks mirror CI:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy packages
uv run pytest
uvx reuse lint
uv run heartwood-fixtures fixtures skills evals
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
