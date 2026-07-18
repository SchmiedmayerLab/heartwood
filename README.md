<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

[![GitHub Release](https://img.shields.io/github/v/release/SchmiedmayerLab/heartwood?display_name=tag&include_prereleases&sort=semver)](https://github.com/SchmiedmayerLab/heartwood/releases)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

Heartwood is an open-source coding agent from the Schmiedmayer Lab at Stanford University for biomedical research environments. Researchers can describe an analysis in natural language, review the proposed actions, and continue the same project from a terminal, browser, or notebook.

Heartwood runs where the project files already reside. It adds repository-verified Skills for synthetic biomedical reference workflows, model controls, action confirmation, and a content-minimized audit record around each agent session.

## What Heartwood Provides

- A conversation-first coding workflow for one clearly bounded project directory.
- Interactive terminal and browser interfaces plus a notebook bridge.
- Connections to research-environment models, hosted providers, existing local services, and downloaded local models.
- Review of proposed command and file-action groups before execution.
- Repository-verified Skills for synthetic reference workflows and explicitly installed project extensions.
- Persistent sessions, replay, and scrubbed audit export.
- Published container images, a native installer, and dedicated Terra and Stanford Carina workflows.

![Heartwood browser interface showing a synthetic analysis](docs/assets/web-reference-analysis.png)

## Quick Start with the Current Preview

The container is the shortest route on macOS or Linux with Docker Engine or Docker Desktop running. It includes Heartwood and the browser interface, but no model weights or credentials. Allow enough disk for the image and project; local-model files require additional space.

```bash
mkdir heartwood-demo
cd heartwood-demo

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3 \
  heartwood serve --host 0.0.0.0
```

Open [http://127.0.0.1:8767/](http://127.0.0.1:8767/), choose an authorized hosted model or existing model service, and start a conversation. Heartwood treats the mounted directory as the project it may modify. To download and run a model in the container instead, follow [Run a Model Locally](docs/getting-started-offline.md); downloaded models start with `heartwood launch --web`, not `heartwood serve`.

## Choose a Setup

[Choose an Environment](docs/platforms.md) compares the workstation container, native Linux installation, Terra image, Stanford Carina installation, and operator-managed deployments. The [preview documentation](https://schmiedmayerlab.github.io/heartwood/preview/) matches this prerelease; stable release documentation remains available from the version selector.

## Responsible Use

Begin with synthetic or non-sensitive files. Installing Heartwood does not make a computer, model provider, or research platform suitable for controlled data. The deploying institution remains responsible for identity, storage, networking, model-provider agreements, dataset permissions, and export controls.

Agent terminal actions run with the permissions of the Heartwood process. Review proposed actions and use an appropriate platform sandbox when the project requires a stronger operating-system boundary.

Heartwood is under active pre-1.0 development. See [Supported Environments](docs/platform-support.md) for the current interface and deployment boundaries. Planned work is tracked in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2).


## Contributing

Contributions to this project are welcome. Please make sure to read the [contribution guide](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [Contributor Covenant Code of Conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first.

The technical ownership and reuse boundaries are documented in [System Architecture](design/03-architecture.md).


## License

This project is licensed under the MIT License. See [Licenses](LICENSES) and [Contributors](CONTRIBUTORS.md) for more information.


## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-light.png#gh-light-mode-only)
![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-dark.png#gh-dark-mode-only)