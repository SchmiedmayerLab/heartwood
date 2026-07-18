<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Install Heartwood

Use the container on a workstation when possible. Choose the native installer for a Linux environment that cannot run Docker or must integrate with host storage and scheduling.

## Choose Container or Native

| Requirement | Container | Native installation |
|---|---|---|
| Minimal host setup | Recommended | Requires command-line dependencies |
| Terminal | Included | Included |
| Browser | Included | Not included in the published native artifact |
| Notebook bridge | Included | Python API included; Jupyter integration is operator-configured |
| CPU local-model server | Included | Install a compatible `llama-server` separately |
| NVIDIA local-model server | Use the explicit GPU image | Included only in the Carina installation |
| Host scheduler integration | No | Yes, on supported platforms |

[Run Heartwood in a Container](container-images.md) provides the normal workstation path.

## Install a Published Release on Linux

The generic native installer requires `curl`, `tar`, `sha256sum`, [uv](https://docs.astral.sh/uv/), and at least 8 GiB of free space. Keep the installation outside every project the agent may modify.

```bash
mkdir -p "$HOME/.local/heartwood"
cd "$HOME/.local/heartwood"

curl --fail --location --output heartwood-installer \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0-beta.3/heartwood-installer
chmod +x heartwood-installer
./heartwood-installer --platform generic
rm heartwood-installer
```

Add the installed command to your shell path:

```bash
export PATH="$HOME/.local/heartwood/bin:$PATH"
heartwood --version
```

Add the same `PATH` entry to the shell configuration only after verifying the installation location. The installer keeps versions side by side and points `bin/heartwood` at the selected release.

Create or enter a separate project directory:

```bash
mkdir -p "$HOME/heartwood-projects/first-project"
cd "$HOME/heartwood-projects/first-project"
heartwood doctor
heartwood
```

Use a hosted or existing model service with the generic native installation unless a compatible `llama-server` is already available on the host. The installer does not add a CPU inference server, built browser assets, or a Jupyter kernel. Use the terminal unless an operator has exposed the installed Heartwood Python API from a compatible Jupyter environment.

## Install from Source for Development

Contributors should use the locked repository environment instead of the release installer. Follow [Contributing](../CONTRIBUTING.md) for the development setup, web dependencies, tests, and documentation preview.

## Update Heartwood

Download the installer attached to the new release and run it from the same installation directory. Review that release's notes before updating a project. Heartwood is pre-1.0 and may reject state created by an incompatible earlier release instead of silently migrating it.
