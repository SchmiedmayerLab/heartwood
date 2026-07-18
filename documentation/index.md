<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

Heartwood is a coding agent for biomedical research environments. It works inside one project directory, lets you review proposed actions, and keeps the same model settings and session history available from the terminal, browser, and notebook.

## Start with Your Environment

| Where you are working | Recommended path |
|---|---|
| A workstation with Docker | [Run Heartwood in a Container](../docs/container-images.md) |
| A Linux server without Docker | [Install Heartwood](../docs/installation.md) |
| A Terra workspace | [Use Heartwood on Terra](../docs/terra-jupyter-demo.md) |
| Stanford Carina | [Use Heartwood on Stanford Carina](../docs/carina-cli.md) |
| An existing institutional deployment | [Get Started](../docs/getting-started.md) |

The container is the easiest local starting point because it includes the command, browser interface, coding-agent runtime, bundled Skills, and CPU inference software. It does not include model weights or credentials. Terra uses a dedicated image that preserves Jupyter, while Carina uses a native installation that can request scheduled GPU compute.

## Learn the Core Workflow

Every setup follows the same sequence:

1. Open the directory Heartwood may modify.
2. Start the terminal or browser interface available in that environment.
3. Connect an authorized model in that interface.
4. Ask for a specific result.
5. Review the complete proposed action group.
6. Inspect the result and audit activity.

[Get Started](../docs/getting-started.md) walks through the sequence. [Work with Heartwood](../docs/using-heartwood.md) explains effective requests, action review, sessions, Skills, and audit export.

## Choose How You Interact

- **Terminal:** the most reliable interface for setup, remote environments, local-model startup, and repeated agent work.
- **Browser:** visual model setup, conversation, action review, Skills, and audit activity.
- **Notebook:** a bridge for submitting tasks and reviewing a project that already has a usable local or operator-provided model connection.

[Terminal, Browser, and Notebook](../docs/web-interface.md) explains how the three interfaces share one project and when each is available.

## Choose Where the Model Runs

Heartwood's guided setup can connect to:

- a model on the current machine, including one downloaded from Hugging Face;
- OpenAI;
- Anthropic; or
- Stanford AI API Gateway.

Generic deployments can also configure another compatible service. Interactive credentials remain in the terminal or browser process where they were entered.

[Choose a Model](../docs/model-connections.md) compares these options. [Run a Model Locally](../docs/getting-started-offline.md) explains downloads, CPU and GPU inference, resource checks, and offline use.

## Understand the Boundary

Heartwood runs where the project files already reside, but it does not authorize a model, dataset, computer, or export. Begin with synthetic or non-sensitive data. The deploying institution remains responsible for platform access, storage, networking, provider agreements, and data-use rules.

For deeper detail, read [Deployment](../docs/deployment.md), [Security and Data Boundaries](../design/05-security-compliance.md), or the [Glossary and Acronyms](../ACRONYMS.md).

## Find an Answer

| Question | Page |
|---|---|
| Which setup should I use? | [Choose an Environment](../docs/platforms.md) |
| What files may Heartwood modify? | [Projects and Persistent State](../docs/project-state.md) |
| Which commands are available? | [Command Reference](../docs/cli-reference.md) |
| Why is setup or a model not ready? | [Troubleshooting](../docs/troubleshooting.md) |
| Which environments and interfaces are available? | [Supported Environments](../docs/platform-support.md) |
| How does Heartwood work internally? | [Architecture Overview](../design/01-overview.md) |


## Contributing

Contributions to this project are welcome. Please make sure to read the [contribution guide](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [Contributor Covenant Code of Conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first.


## License

This project is licensed under the MIT License. See [Licenses](LICENSES) and [Contributors](CONTRIBUTORS.md) for more information.


## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-light.png#gh-light-mode-only)
![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-dark.png#gh-dark-mode-only)