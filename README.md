<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

[![GitHub Release](https://img.shields.io/github/v/release/SchmiedmayerLab/heartwood?display_name=tag&sort=semver)](https://github.com/SchmiedmayerLab/heartwood/releases/latest)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

Heartwood is a coding agent for biomedical research environments. Researchers can describe an analysis in natural language, review the actions the agent proposes, and continue the same project from a terminal, browser, or notebook.

Heartwood runs alongside the project instead of moving it into a separate service. It adds reviewed biomedical guidance, model controls, action confirmation, and an audit record around the agent session.

## What You Can Do

- Ask Heartwood to inspect, create, and update files in one analysis project.
- Review proposed commands and file changes before they run.
- Use reviewed biomedical Skills that provide domain-specific procedures.
- Connect an institution-provided model, a common hosted provider, an existing local service, or a model that runs on the same machine.
- Leave and return without losing the conversation or project setup.
- Export a content-minimized record of model-route decisions, action reviews, and tool outcomes.

![Heartwood showing a synthetic reference analysis in the browser](docs/assets/web-reference-analysis.png)

## Start Here

Heartwood treats the directory where it starts as the project and keeps its setup and conversation history with that project.

Getting to the first task involves three decisions:

1. **Where Heartwood runs:** a container, a native installation, or a managed platform image.
2. **Where the model runs:** in the research environment, on the same machine, or through an authorized provider.
3. **How you interact:** the terminal, browser, or notebook.

Choose the installation path that matches your environment:

| Your environment | Recommended path |
|---|---|
| Personal workstation or general-purpose server with Docker | Use the [Heartwood container](docs/getting-started.md#use-the-container). It is the shortest path to the complete terminal and browser experience. |
| Existing managed environment without Docker | Use the [native installation](docs/getting-started.md#install-the-command). |
| Terra | Use the [Terra image and Jupyter workflow](docs/terra-jupyter-demo.md). |
| Stanford Carina | Use the [Carina installation and terminal workflow](docs/carina-cli.md). |

Then enter the directory Heartwood may work on and start Heartwood:

```bash
cd /path/to/analysis-project
heartwood
```

The first run guides you through choosing a model and opens the conversation when the model is available. A downloaded local model instead directs you to `heartwood launch`, which starts and supervises its inference server. Follow [Get Started](docs/getting-started.md) for the complete first-use path, or use [Troubleshooting](docs/troubleshooting.md) when Heartwood reports that setup or compute is still required.

## Choose a Model

Heartwood does not include model weights or provider credentials. During setup, choose where the model should run:

- **Research environment:** use a model supplied by the platform or institution.
- **On this device:** connect an existing model service, choose a recommended local model, or provide another Hugging Face model identifier.
- **OpenAI or Anthropic:** enter a token for the current Heartwood process and choose a model returned by the provider.
- **Custom API:** connect another service that follows the OpenAI API format.

Start with [Connect a Model](docs/model-connections.md). [Run a Model Locally](docs/getting-started-offline.md) explains model downloads, the local model server, CPU and GPU choices, and offline operation.

## Use the Same Project Everywhere

The terminal and browser offer the same conversations, model selection, action review, Skills, and audit history. The notebook bridge can inspect and continue that shared state without creating another project configuration.

- [Work with the Agent](docs/using-heartwood.md) explains conversations and action review.
- [Use the Browser and Notebooks](docs/web-interface.md) explains the visual interface and Jupyter proxy workflow.
- [Project Files and State](docs/project-state.md) explains the project boundary, persistence, and migration.

## Use Sensitive Data Responsibly

Heartwood is designed to run where research data already resides, but installing Heartwood does not make a laptop, container, model provider, or research platform suitable for protected data. The deploying institution remains responsible for identity, storage, networking, model-provider agreements, dataset permissions, and export controls.

Agent terminal actions run with the permissions of the Heartwood process. Review proposed actions and use an appropriate platform sandbox when the project requires a hard operating-system boundary.

[Platform Support and Validation](docs/platform-support.md) distinguishes implemented software, automated evidence, live-platform validation, and institutional approval.

## Learn More

- [Choose Where to Run Heartwood](docs/platforms.md) compares workstation, container, Terra, Carina, and other managed environments.
- [Deploy Heartwood](docs/deployment.md) introduces deployment artifacts and operator responsibilities.
- [How Heartwood Works](design/01-overview.md) begins the technical architecture and rationale.
- [Glossary and Acronyms](ACRONYMS.md) explains specialized terminology.
- [Documentation Guide](docs/README.md) indexes all current documentation.

Planned work and acceptance criteria live in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2).

## Contribute

See [Contributing](CONTRIBUTING.md) for the development environment, test commands, repository conventions, and review expectations.

The technical ownership and reuse boundaries are documented in [System Architecture](design/03-architecture.md).

## License

Heartwood is released under the MIT License. See [LICENSE](LICENSE), [CONTRIBUTORS.md](CONTRIBUTORS.md), and [NOTICE](NOTICE).

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).
