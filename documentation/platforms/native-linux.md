<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Install Natively on Linux

Use the native release installer on a compatible Linux host when containers are unavailable or conflict with the platform's normal execution model.
The container remains the simpler workstation path because it fixes the operating-system dependencies as part of the image.

## Before You Begin

You need a writable installation directory, `curl`, `tar`, a SHA-256 checksum utility, and [uv](https://docs.astral.sh/uv/) on `PATH`.
Keep the installation outside the research project so application files and project state have independent lifecycles.

## Install a Release

```bash
mkdir -m 700 heartwood-installation
cd heartwood-installation

curl --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0-beta.3/heartwood-installer
chmod 700 heartwood-installer
./heartwood-installer --platform generic
export PATH="$PWD/bin:$PATH"
```

The version-stamped installer downloads the matching native archive and checksum, verifies both, checks available storage, installs the locked environment, and publishes the `heartwood` command.
It reports seven named stages; dependency installation can take several minutes.

## Open a Project

Create or enter a separate project directory, then start Heartwood there:

```bash
mkdir ../heartwood-project
cd ../heartwood-project
heartwood doctor
heartwood
```

The current directory is the project and `.heartwood/` inside it holds private configuration, sessions, downloaded models, and audit data.
Run `heartwood --interface web` for the browser interface.

The native package includes the Python notebook bridge, but it does not start Jupyter for you.
Use [notebook interaction](../use/notebooks.md) only from a Jupyter kernel that runs in the installed Heartwood environment and starts in the intended project directory.

## Update

Download the installer from the new release and run it from the same installation directory.
The installer creates a version-scoped runtime and updates the `current` link only after the new installation succeeds.
Project files and `.heartwood/` are not stored in the installation directory and remain unchanged.
