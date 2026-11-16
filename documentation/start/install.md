<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Install Heartwood

Install Heartwood where your project files already reside.
Each route below produces a local launch: the `heartwood` command, with project state in `.heartwood/` inside the folder where you run it.
An isolated-worker deployment is set up by an operator; see [Deploy Heartwood](../operate/index.md).

## Choose an Installation

| Situation | Use | What You Install |
|---|---|---|
| You have Docker Desktop or Docker Engine | [Container](../platforms/containers.md) | One versioned Heartwood image |
| You use Terra | [Terra image](../platforms/terra.md) | A versioned image selected in Terra |
| You use Stanford Carina | [Native installer for Carina](../platforms/carina.md) | A release-scoped installation in project storage |
| You use Linux without Docker | [Native installer for Linux](../platforms/native-linux.md) | A release-scoped installation using `uv` |
| You change Heartwood itself | [Development installation](#development-installation) | The repository and locked development dependencies |

Images and installers contain inference software but no model weights and no credentials.

## Confirm the Installation

```bash
heartwood --version
heartwood doctor
```

The first command prints the installed release.
The second inspects the current directory and environment without changing either.

## Development Installation

Use this route only to change Heartwood.
It requires Git, [uv](https://docs.astral.sh/uv/) 0.13.7, and Node.js for browser-interface work.

```bash
git clone --recurse-submodules https://github.com/SchmiedmayerLab/heartwood.git
cd heartwood
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --bootstrap
uv sync --locked --all-groups --all-extras
uv run heartwood --version
```

The bootstrap builds the patched OpenHands and OpenShell wheels that the lock references under `vendor/runtime-wheels/`; a registry-only installation cannot resolve those local versions.
See the [Development Guide](../contribute/development.md) for tests and repository structure.

## Update or Remove Heartwood

Container users select a new versioned image tag and remove old images through normal Docker maintenance.
Native installations are release-scoped: run the new release's installer in the same installation directory, and it switches the `current` link only after the new installation passes its startup checks.

`.heartwood/` belongs to each project, not to the installation.
Do not copy it into an installation directory.
