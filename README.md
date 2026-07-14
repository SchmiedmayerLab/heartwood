<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

[![GitHub Release](https://img.shields.io/github/v/release/SchmiedmayerLab/heartwood?display_name=tag&sort=semver)](https://github.com/SchmiedmayerLab/heartwood/releases/latest)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

Heartwood is an auditable coding-agent environment for biomedical research. It combines domain-specific guidance, model-route policy, action confirmation, platform awareness, and a scrubbed audit trail. Researchers can use the same project through an interactive terminal, a web interface, or a notebook.

Heartwood is designed to run close to the data. It does not make a laptop, container, or model provider suitable for protected health information by itself; the deploying institution remains responsible for identity, storage, network, provider, agreement, and data-use controls.

## What Heartwood Does

- Works with the files in the directory where you start it.
- Provides a conversational coding agent that can inspect, create, and modify project files after the required review.
- Loads reviewed biomedical Skills that give the agent domain-specific procedures.
- Lets you review an OpenHands action set before it runs, or automatically allow only actions OpenHands classifies as low risk when deployment policy permits that mode.
- Connects to approved research services, common hosted providers, an existing local model service, a recommended local model, or another supported Hugging Face model you identify.
- Preserves conversations and project settings across restarts.
- Produces a content-minimized, tamper-evident audit export.

## Get Started

Heartwood always treats the current directory as the project. Change into the analysis directory first, then run Heartwood. It does not search for a Git repository or require state-path options.

### Use the Container

The container is the shortest path for a workstation because it already includes the command, web interface, coding-agent runtime, and CPU local-inference software. It contains no model weights and no credentials.

```bash
mkdir heartwood-demo
cd heartwood-demo
docker pull ghcr.io/schmiedmayerlab/heartwood:0.2.0
docker run --rm -it \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood serve --host 0.0.0.0
```

Open `http://127.0.0.1:8767/`, configure an authorized model, and start a conversation. The mounted directory is the project, so approved agent changes appear directly in `heartwood-demo`. If you download a local model in the browser, Heartwood shows its progress and then tells you to restart the container with `heartwood launch --web` so it can supervise both the model and interface. See [Container Images](docs/container-images.md) for that command, Linux file-permission guidance, persistent named volumes, and hardened deployment options.

### Install the Command

Use the native release when Docker is unavailable or when Heartwood must run inside an existing managed environment such as Stanford Carina.

```bash
curl --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0/heartwood-installer
chmod +x heartwood-installer
./heartwood-installer \
  --root "$HOME/.local/share/heartwood" \
  --version 0.2.0
export PATH="$HOME/.local/share/heartwood/bin:$PATH"
```

Then start or resume any project from that project's directory:

```bash
mkdir analysis-project
cd analysis-project
heartwood
```

The first run guides you to a model connection. Later runs reopen the same project setup and default session. `heartwood doctor` performs a read-only readiness check, and `heartwood --help` lists focused commands for models, Skills, actions, replay, and audit export.

### Use a Research Platform

- [Stanford Carina](docs/carina-cli.md) uses the native installer and lets `heartwood launch` request an explicitly confirmed Slurm allocation for a local model.
- [Terra](docs/terra-jupyter-demo.md) uses the Terra-derived image so Jupyter and Leonardo routing continue to work normally.
- [Platform Support](docs/platform-support.md) distinguishes repository implementation, CI validation, live validation, and institutional approval.

## Choose a Model

Heartwood does not ship model weights. The model setup presents only information needed for the selected connection:

- **Research environment:** choose a model made available by the platform administrator; credentials may already be managed by the environment.
- **On this device:** use an existing OpenAI-compatible service, choose from a short centrally maintained recommendation list, or enter another Hugging Face `owner/model` identifier. Heartwood inspects the repository, selects a supported CPU or NVIDIA GPU representation for the current deployment, and shows storage and compute guidance before download.
- **OpenAI or Anthropic:** enter a token through the hidden terminal prompt or running web interface and choose a model returned by the provider's own catalog. Whether that provider may receive controlled data is a separate institutional decision.
- **Custom API:** provide the base URL, optional token, and a model exposed by an OpenAI-compatible service.

Recommended means that Heartwood maintains reproducible download and runtime metadata for an approachable default; it is not a model-quality, biomedical-use, license-approval, or institutional-approval claim. User-selected repositories are supported on a best-effort basis. Unsupported or ambiguous formats fail before download and link to the GitHub issue chooser.

For a fully local setup, see [Local and Offline Models](docs/getting-started-offline.md). For provider, platform, and credential behavior, see [Choose a Model](docs/model-connections.md).

## Work in a Project

The interactive command is the primary terminal experience:

```bash
heartwood
```

Enter a task in natural language. When the agent pauses for confirmation, Heartwood displays the complete action set together. Choose **Allow all once** only when every member is appropriate, or **Reject all**. Use `/help` in the terminal for session controls and [Use Heartwood](docs/using-heartwood.md) for the shared CLI, web, and notebook workflow.

Heartwood creates a private `.heartwood/` directory inside the project. It contains configuration, sessions, downloaded models, installed Skills, audit data, runtime records, logs, and caches. Do not edit it manually or give the agent tasks that target it. To work on another project, change directories and start Heartwood there.

## Safety Boundary

Participant-level data must remain inside an institution-approved deployment boundary. Source control, examples, public documentation, screenshots, and continuous integration use synthetic fixtures only. A model appearing in the interface means that Heartwood can describe or reach the configured route; it does not establish a business associate agreement, Health Insurance Portability and Accountability Act eligibility, or approval for a particular dataset.

OpenHands terminal commands run with the permissions of the Heartwood process. Heartwood constrains its own file APIs to the project, but a hard operating-system boundary for arbitrary commands requires a platform sandbox or supported OpenHands remote workspace. Review proposed actions and deployment controls accordingly.

## Documentation

- [Use Heartwood](docs/using-heartwood.md) is the first-use guide.
- [Work with Heartwood in a Browser](docs/web-interface.md) covers first-run setup, shared project state, model preparation, and conversations.
- [Local and Offline Models](docs/getting-started-offline.md) explains downloads, runtimes, and no-network validation.
- [Platform Support](docs/platform-support.md) records what is implemented and how it has been validated.
- [System Architecture](design/03-architecture.md) and [Security and Compliance](design/05-security-compliance.md) define the technical and security contracts.
- [Documentation](docs/README.md) indexes all operational and technical material.
- [Acronyms](ACRONYMS.md) expands project terminology.

Planned work and acceptance criteria live in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2), not in release documentation.

## Development

[![Python](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/python.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/python.yml)
[![Validate](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/validate.yml)
[![CodeQL](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/codeql.yml)
[![Secret Scan](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/gitleaks.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/gitleaks.yml)
[![Dependency Review](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/dependency-review.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/dependency-review.yml)

Heartwood uses Python 3.12, `uv`, the pinned OpenHands SDK, and a TypeScript web interface built on Stanford Spezi Web Design System packages.

```bash
uv sync --locked --all-extras --all-groups --python 3.12
uv run heartwood --version
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy packages
```

Run development commands from the repository root when the repository itself should be the Heartwood project. The same `.heartwood/` contract applies. See [Development Practices](design/08-development.md) and [Contributing](CONTRIBUTING.md).

## License

Heartwood is released under the MIT License. See [LICENSE](LICENSE), [CONTRIBUTORS.md](CONTRIBUTORS.md), and [NOTICE](NOTICE).

## Our Research

This project is based on work from the [Stanford Biodesign Digital Health group](https://biodesigndigitalhealth.stanford.edu) at Stanford University.
