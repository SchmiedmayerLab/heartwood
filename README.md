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

Heartwood is designed to run inside trusted research platforms, close to controlled data. It provides platform detection, policy checks, skill packaging, audit records, and CLI, notebook, and web UI workflows for reproducible analyses, built on one session command/event contract.

Participant-level data stays inside the platform boundary. Development and CI use synthetic fixtures only.


## Overview

Heartwood builds the biomedical, platform, policy, skills, and audit layer around a reusable execution core. The architecture centers on a session gateway that owns the local agent-server boundary and exposes one shared session command/event contract to shipped surfaces such as the CLI, notebook bridge, and researcher web UI (see [design/03-architecture.md](design/03-architecture.md)). The project uses a Python workspace, typed contracts, platform adapters, verified local skills, and deterministic offline harnesses for local development and CI.

The current repository contains the core foundation: repository health files, CI, the `uv` workspace, deterministic platform detection, adapter protocols and generic/local adapters, versioned schemas, synthetic fixture checks, deny-by-default model policy, hash-chained audit logging, resumable session orchestration, local skill verification, a bundle catalog for packaged skills, prototype verified skills, replay fixtures, the session gateway package, the expanded `heartwood` command-line interface, a notebook bridge, a Spezi-based researcher web UI, synthetic evidence-bundle generation, generic image smoke-test configuration, the implemented `llama-cpp-cpu` smoke profile, provider route invocation, image flavor metadata, and pinned OpenHands agent-server packaging. The full implementation plan is tracked in [design/09-implementation-plan.md](design/09-implementation-plan.md).


## Usage

Install [`uv`](https://docs.astral.sh/uv/) and run the local commands from a repository checkout:

```bash
uv sync
uv run heartwood --version
uv run heartwood detect
uv run heartwood chat --prompt "summarize the synthetic cohort"
uv run heartwood run --endpoint https://model.local.invalid/v1/chat/completions
uv run heartwood audit export
uv run heartwood reviewer packet
uv run heartwood serve --web-root packages/webui/dist
```

The `detect` command inspects environment markers, fingerprints the local synthetic fixture by filenames and headers only, and prints a proposal. The `chat`, `run`, `replay`, `audit export`, reviewer-packet, notebook, and web UI paths use the same session command/event contract.

Run the generic offline stack from Docker only after the main-branch images are published. The default image family is published for `linux/amd64` and `linux/arm64` where the dependency stack supports both platforms:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:edge
docker run --rm -p 8767:8767 ghcr.io/schmiedmayerlab/heartwood:edge bash images/generic/scripts/start_demo_stack.sh
```

Open `http://127.0.0.1:8767/`, click **Run Local Model**, then inspect the Conversation, Local Model, Policy, Approvals, Activity, and Exports panels. The Conversation panel shows the current browser-session prompt, the local model response preview, the agent message, and event-derived trace summaries. The demo stack starts the bundled Qwen2.5-Coder-7B llama.cpp model, starts the gateway-managed OpenHands child server, pre-approves the synthetic model-call decision for the default local session, and enables the bounded synthetic response preview for the UI.

The `edge` runtime image carries the CLI, gateway, notebook bridge, built researcher web UI, local inference runtime dependencies, provider route validation/invocation support, pinned OpenHands agent-server package, policy/audit stack, and the bundled Qwen2.5-Coder-7B-Instruct Q4_K_M local coding model. The model-specific `edge-coder-7b` tag is an alias for the same default image. The `edge-smoke` image keeps the tiny verified GGUF artifact for CI smoke only. The `edge-providers` image carries provider route configuration support with file-based runtime secret references and no provider secrets or model weights. See [Container Images](docs/container-images.md) for the tag scheme and flavor policy.

From a checkout, run the same CI smoke path with Compose:

```bash
docker compose -f images/generic/compose.yaml run --rm heartwood
```

To run the CI smoke path with the tiny model artifact instead, use the smoke image with runtime network disabled:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:edge-smoke
docker run --rm --network none ghcr.io/schmiedmayerlab/heartwood:edge-smoke bash images/generic/scripts/offline_stack_smoke.sh
```

See [Getting Started With The Offline Stack](docs/getting-started-offline.md) for the full walkthrough and current limitations.

For Terra-like notebook demonstrations, see [Terra-Style Jupyter Demo](docs/terra-jupyter-demo.md). The main-branch image workflow publishes public Terra-derived `edge-terra` and model-specific alias `edge-terra-coder-7b` notebook images with the bundled Qwen2.5-Coder-7B model, publishes `edge-terra-smoke` for tiny-model CI smoke, verifies anonymous registry access before succeeding, and CI validates the same platform Dockerfile through a lightweight Terra-compatible base plus local Terra-style proxy mechanics; a live Terra workspace smoke is still required before claiming supported Terra launch behavior with platform identity binding. The extension mechanism for Terra variants and future platform-derived notebook images is documented in [Platform Image Extension Guide](docs/platform-images.md).


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
- [`packages/webui`](packages/webui) contains the Spezi-based researcher web UI.
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

The web UI checks run from `packages/webui`:

```bash
npm ci
npm run format:check
npm run lint
npm run typecheck
npm run test
npm run build
npm run license:check
npm run test:e2e
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
| [Container Images](docs/container-images.md) | Image flavors, tags, provider secrets, model strategy |
| [Platform Image Extension Guide](docs/platform-images.md) | Mechanism for adding or adapting Terra-like platform notebook images |
| [Terra-Style Jupyter Demo](docs/terra-jupyter-demo.md) | Synthetic CLI, notebook, and web UI demo path for Terra-like workspaces, with a companion [notebook](docs/terra-jupyter-demo.ipynb) |

## Contributing

Contributions to this project are welcome. Please make sure to read the [contribution guide](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [Contributor Covenant Code of Conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first.

Because Heartwood runs next to controlled data, contributors must never include PHI, credentials, or live-platform identifiers in issues, pull requests, tests, or fixtures; all fixtures are synthetic. Project-direction changes update the relevant [`design/`](design) document first, and security- or compliance-relevant claims must be backed by tests, audit records, or a documented limitation.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for more information.

## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-light.png#gh-light-mode-only)
![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-dark.png#gh-dark-mode-only)
