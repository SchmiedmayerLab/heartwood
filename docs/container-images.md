<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Run Heartwood in a Container

The generic container is the recommended workstation setup. It includes Heartwood, the browser application, bundled Skills, OpenHands, and the CPU inference runtime. It contains no model weights or credentials.

You need Docker Engine or Docker Desktop, enough disk for the image and project, and a Bash-compatible shell on macOS or Linux. The examples use Unix ownership and mount syntax. On Windows, run them from Windows Subsystem for Linux or adapt the user and bind-mount options for the local Docker setup.

## Choose an Image

| Image | Architecture | Use |
|---|---|---|
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3` | AMD64 and ARM64 | Hosted models, existing services, or portable CPU inference |
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-gpu-nvidia` | AMD64 | Local inference on a compatible NVIDIA host |
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra` | AMD64 | Terra with hosted models or portable CPU inference |
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra-gpu-nvidia` | AMD64 | Terra with local NVIDIA inference |

Use the non-Terra images on ordinary Docker hosts. Terra images preserve Terra's Jupyter runtime and are selected through the Terra cloud-environment configuration.

## Start the Browser

Create or enter the host directory the agent may modify:

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

Open `http://127.0.0.1:8767/`. Complete model setup in the browser and keep the container running while you work.

The host directory is mounted at `/workspace`, the image's starting directory. Heartwood treats it as the project and keeps project state in that mount, so source files, settings, sessions, and downloaded models survive replacement containers.

The command maps the container process to the current host user for bind-mount compatibility. Managed deployments should instead prepare project ownership for a reviewed non-root identity.

## Start the Terminal

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3 \
  heartwood
```

## Run a Local CPU Model

Download a listed public model into the mounted project:

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3 \
  heartwood models download qwen25-7b-instruct-q4_k_m
```

Then start the model and browser:

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3 \
  heartwood launch --web --host 0.0.0.0
```

The portable image runs llama.cpp on CPU. A 7B model needs substantial disk and memory and may take several minutes per agent turn. Review the resource plan before downloading.

## Run a Local NVIDIA Model

Use the explicit GPU image, Docker's NVIDIA runtime, and a model listed for vLLM:

```bash
docker run --rm -it \
  --gpus all \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-gpu-nvidia \
  heartwood launch --web --host 0.0.0.0
```

Download the compatible GPU model through the same image before launch. The host needs a supported NVIDIA driver and enough GPU memory for the model and selected context. Heartwood reports an error instead of silently falling back to CPU.

## Preserve and Protect the Project

- Keep the complete host project directory, including its hidden Heartwood state.
- Do not place provider tokens in image layers, build arguments, labels, or project files.
- Bind the browser to loopback for local use; use an authenticated platform proxy for remote access.
- Add deployment-specific read-only filesystems, dropped capabilities, process limits, and egress controls where required.

[Run a Model Locally](getting-started-offline.md) explains model inspection and offline use. [Deployment](deployment.md) covers operator responsibilities.
