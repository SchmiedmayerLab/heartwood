<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Run Heartwood in a Container

The Heartwood container is the recommended first installation on a macOS or Linux workstation.
It packages the `heartwood` command, the browser interface, OpenHands, repository-verified Skills and managed inference software.
It contains no model weights and no credentials, and it is not set up for notebooks; use the terminal or browser, or a Terra image for notebooks.

## Prerequisites

Install Docker Desktop or Docker Engine and confirm that `docker version` succeeds.
Create a dedicated host folder for the project; the bind mount makes it `/workspace` inside the container.

## Start the Terminal

```bash
mkdir heartwood-project
cd heartwood-project

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.5.1 \
  heartwood
```

The user mapping keeps files created in the project owned by your host user.
The container is temporary; the mounted folder and its `.heartwood/` state persist on the host.
After a Heartwood-managed model finishes downloading, the terminal continues into the session.
If the container stops during setup, repeat the same command; project state and verified downloads are reused.

### Keep a ChatGPT Sign-In

With `HOME=/tmp`, a ChatGPT sign-in lasts only until the container stops.
To reuse it without placing credentials in the project, mount a private host directory as OpenHands' credential directory:

```bash
mkdir -p "$HOME/.local/share/heartwood/openhands-auth"
chmod 700 "$HOME/.local/share/heartwood/openhands-auth"

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  -v "$HOME/.local/share/heartwood/openhands-auth:/tmp/.openhands/auth" \
  ghcr.io/schmiedmayerlab/heartwood:0.5.1 \
  heartwood
```

Treat that directory as a secret: keep it out of repositories, shared project folders and images.

## Start the Browser

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.5.1 \
  heartwood --interface web --host 0.0.0.0 --host-loopback-publication
```

Open the launch link the container prints and keep the container running; the page continues at `http://127.0.0.1:8767/`.
The process listens on the container interface, but `-p 127.0.0.1:8767:8767` publishes it only on host loopback.
`--host-loopback-publication` is your assertion that this is the case; Heartwood refuses a wildcard bind without it and still accepts only loopback or private bridge sources.
Do not publish the port on a shared network address or put an unauthenticated proxy in front of it.
When the setup page reports that a managed model is downloaded, stop the container with `Ctrl-C` and run the command again to load the model.

## Use an NVIDIA GPU

Install the NVIDIA Container Toolkit and confirm that Docker can expose the GPU, then use the GPU image:

```bash
docker run --rm -it \
  --gpus all \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.5.1-gpu-nvidia \
  heartwood
```

Choose **Run with Heartwood** and a vLLM-compatible model.
Heartwood inspects the GPU model, memory and driver, selects a conservative context size, and starts the CUDA 12.9 vLLM runtime once the model is ready.
No GPU model configuration is qualified for a generic workstation; the [GPU compatibility matrix](../reference/gpu-compatibility.md) lists the qualified Terra and Carina configurations.

## Image Tags

Use immutable release tags for research work:

| Tag | Image | Architectures |
|---|---|---|
| `ghcr.io/schmiedmayerlab/heartwood:0.5.1` | Standard | AMD64, ARM64 |
| `ghcr.io/schmiedmayerlab/heartwood:0.5.1-gpu-nvidia` | NVIDIA GPU | AMD64 |
| `ghcr.io/schmiedmayerlab/heartwood:0.5.1-terra` | Terra | AMD64 |
| `ghcr.io/schmiedmayerlab/heartwood:0.5.1-terra-gpu-nvidia` | Terra NVIDIA GPU | AMD64 |

The moving `edge` tags follow `main` and are for development, not reproducible analyses.
Release publication promotes only the candidate digests it verified for the release commit.

## Isolation Limits

A container launch is a local launch.
Agent tools run as the container user with full access to the mounted folder, including `.heartwood/`, and to the container's network.
The container is packaging, not an authorization boundary: add read-only mounts, network controls and resource limits according to the deployment's threat model, and use platform secrets or mounted credential files rather than tokens in an image or command.
The isolated worker is a separate [operator-bound deployment](../start/project.md#operator-bound-external-state).
