<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Install Heartwood

Choose one installation route for the environment where your project files already reside.
Containers are the easiest workstation route because they package Heartwood, the browser interface, and managed inference software without adding those dependencies to the host.
The native installer is intended for compatible Linux hosts and research systems such as Stanford Carina when containers are not the normal execution mechanism.

## Choose an Installation

| Situation | Use | What You Install |
|---|---|---|
| You have Docker Desktop or Docker Engine | [Container](../platforms/containers.md) | One versioned Heartwood image |
| You use Terra | [Terra image](../platforms/terra.md) | A versioned image selected in Terra |
| You use Stanford Carina | [Native release installer](../platforms/carina.md) | A release-scoped installation in project storage |
| You use Linux without Docker | [Generic native installer](../platforms/native-linux.md) | A release-scoped installation using `uv` |
| You contribute to Heartwood | [Development installation](#development-installation) | The repository and locked development dependencies |
| You operate another platform | [Platform integration](../operate/platform-integration.md) | A platform-specific image or native installation |

Heartwood images and installers contain no provider credentials and no model weights.

## Confirm the Installation

After installing, run:

```bash
heartwood --version
heartwood doctor
```

The first command prints the installed release.
The second inspects the current directory and environment without changing either one.

## Development Installation

Use this route only when changing Heartwood itself.
It requires Git, [uv](https://docs.astral.sh/uv/), and Node.js for browser-interface work.

```bash
git clone https://github.com/SchmiedmayerLab/heartwood.git
cd heartwood
uv sync --locked --all-groups
uv run heartwood --version
```

The project uses the locked workspace rather than installing packages individually.
See the [Development Guide](../contribute/development.md) for tests and repository structure.

## Update or Remove Heartwood

Container users select a new versioned image tag and remove old images with their normal Docker maintenance process.
Native release installations are version-scoped, so install the new release beside the previous one, verify it, and then update the executable path.

Do not copy `.heartwood/` into an installation directory.
That folder belongs to each research project and persists independently of the executable.
