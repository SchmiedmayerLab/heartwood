<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Install Natively on Linux

Use the native release installer on a compatible Linux host when containers are unavailable or conflict with the platform's normal execution model.
The container remains the simpler workstation path because it fixes the operating-system dependencies as part of the image.

## Before You Begin

You need a writable installation directory and a recent 64-bit Linux system using an AMD or Arm processor.
Keep the installation outside the research project so application files and project state have independent lifecycles.
Release CI installs and exercises the package from an empty Ubuntu 24.04 AMD64 container and tests the portable ARM64 inference runtime in an ARM64 container.
Other glibc-based distributions may work when they provide compatible system libraries, but they are not release-validated native hosts.

On Debian or Ubuntu, install the operating-system prerequisites with:

```bash
sudo apt-get update
sudo apt-get install ca-certificates coreutils curl git libgomp1 tar tmux
```

If the shell is already running as `root`, omit `sudo` from both commands.

On another compatible distribution, install packages that provide the same commands and libraries, then confirm the runtime can start before relying on the installation.
Then install [uv](https://docs.astral.sh/uv/) by following its official installation instructions.

Confirm the required commands before downloading Heartwood:

```bash
curl --version
git --version
tar --version
sha256sum --version
tmux -V
uv --version
```

Open a new terminal if the `uv` installer requests it, then rerun `uv --version`.

## Install a Release

```bash
mkdir -m 700 heartwood-installation
cd heartwood-installation

curl --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0-beta.5/heartwood-installer
chmod 700 heartwood-installer
./heartwood-installer --platform generic
export PATH="$PWD/bin:$PATH"
```

The version-stamped installer downloads the matching native archive and checksum, verifies both, checks available storage, installs the locked Heartwood environment and CPU inference runtime, and publishes the `heartwood` command.
It reports seven named stages; dependency installation can take several minutes.
No model weights are included.

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
The first setup flow can download a recommended model or another compatible model from Hugging Face and run it with the installed CPU inference runtime.
For GPU inference, use the NVIDIA container image or a supported managed platform instead of this generic native package.

The native package also provides a Jupyter launcher that uses the same locked environment:

```bash
heartwood-jupyter
```

Start it from the project directory, open the URL it prints, and use the **Python 3 (ipykernel)** kernel.
In a disposable container that runs only as `root`, add `--allow-root`; do not use that exception for a normal user installation.
Then follow [notebook interaction](../use/notebooks.md).

## Update

Download the installer from the new release and run it from the same installation directory.
The installer serializes updates to one installation root, assembles each source-and-runtime generation separately, and updates the `current` link only after the new installation passes its startup checks.
Project files and `.heartwood/` are not stored in the installation directory and remain unchanged.
