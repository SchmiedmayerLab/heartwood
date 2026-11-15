<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Install Natively on Linux

Use the native installer on a Linux host when containers are unavailable or conflict with the platform's normal execution model.
The container is the simpler workstation route because the image fixes the operating-system dependencies.

## Before You Begin

You need a 64-bit Linux system on an AMD64 or ARM64 processor and a writable installation directory outside the research project, so that the application and project state have independent lifecycles.
Release CI installs the release assets in an empty Ubuntu 24.04 AMD64 container and exercises the installed command.
Other glibc-based distributions with compatible system libraries can work but are not release-tested.

On Debian or Ubuntu, install the prerequisites:

```bash
sudo apt-get update
sudo apt-get install ca-certificates coreutils curl git libgomp1 tar tmux
```

Omit `sudo` when the shell already runs as `root`.
On another distribution, install packages that provide the same commands and libraries.
Then install [uv](https://docs.astral.sh/uv/) by following its official instructions, and confirm the commands:

```bash
curl --version
git --version
tar --version
sha256sum --version
tmux -V
uv --version
```

Open a new terminal if the `uv` installer asks you to, then rerun `uv --version`.

## Install a Release

```bash
mkdir -m 700 heartwood-installation
cd heartwood-installation

curl --proto '=https' --tlsv1.2 --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.5.0/heartwood-installer
chmod 700 heartwood-installer
./heartwood-installer --platform generic
export PATH="$PWD/bin:$PATH"
```

Where the GitHub CLI is available, `gh attestation verify heartwood-installer --repo SchmiedmayerLab/heartwood` confirms that the release workflow built the installer.
The installer downloads the matching native archive and checksums, verifies them, checks available storage, installs the locked Heartwood environment and the llama.cpp CPU runtime, and publishes the `heartwood` and `heartwood-jupyter` commands.
It reports seven named stages; dependency installation can take several minutes.
No model weights are included.

## Open a Project

```bash
mkdir ../heartwood-project
cd ../heartwood-project
heartwood doctor
heartwood
```

The current directory is the project, and `.heartwood/` inside it holds configuration, sessions, downloaded models and audit records.
Run `heartwood --interface web` for the browser interface on `http://127.0.0.1:8767/`.
Setup can download a recommended model or another compatible Hugging Face model and run it on the CPU runtime.
For GPU inference, use the NVIDIA container image or the Carina installer; the generic native package has no GPU runtime.

The installation also provides a Jupyter launcher in the same locked environment:

```bash
heartwood-jupyter
```

Start it from the project directory, open the URL it prints, and select the **Python 3 (ipykernel)** kernel.
In a disposable container that runs only as `root`, add `--allow-root`; do not use it for a normal user installation.
Then follow [Use Heartwood From a Notebook](../use/notebooks.md).

A native launch is a local launch: agent tools run with your user's permissions, as described in [Projects and Private State](../start/project.md#heartwood-project-state).

## Update

Download the installer from the new release and run it in the same installation directory.
The installer serializes updates to one installation root, assembles each release in a separate generation, and moves the `current` link only after the new generation passes its startup checks.
Project files and `.heartwood/` are outside the installation directory and remain unchanged.
