<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Run Heartwood in a Container

The Heartwood container is the recommended first installation on a macOS or Linux workstation.
It packages the CLI, browser interface, OpenHands SDK and tools, repository-verified Skills, audit stack, and managed inference software without installing those components on the host.
The standard image does not provide Jupyter; use the terminal or browser, or choose a platform image such as Terra that supplies a shared Jupyter environment.

The image contains no model weights and no credentials.

## Before You Begin

Install Docker Desktop or Docker Engine and make sure `docker version` succeeds.
Create a dedicated host folder for the project; the bind mount makes that folder available as `/workspace` inside the container.

## Start the Terminal

```bash
mkdir heartwood-project
cd heartwood-project

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10 \
  heartwood
```

The user mapping keeps files created in the project owned by the host user.
The container is temporary, but the mounted project and `.heartwood/` persist on the host.
The terminal flow continues automatically after a Heartwood-managed model finishes downloading.
If the container was interrupted during setup, repeat the same `docker run` command; the project state and verified download are reused.

## Start the Browser

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10 \
  heartwood --interface web --host 0.0.0.0
```

Open `http://127.0.0.1:8767/` and keep the container running.
The host binding is loopback-only; do not expose the unauthenticated service on a shared network.
When the setup page reports that a managed model is downloaded, stop the container with `Ctrl-C` and repeat this command to load the model and return to the browser.

## Use an NVIDIA GPU

Install the NVIDIA Container Toolkit and verify that Docker can expose the GPU.
Then use the GPU image:

```bash
docker run --rm -it \
  --gpus all \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10-gpu-nvidia \
  heartwood
```

Choose **Run with Heartwood** and a vLLM-compatible model.
Heartwood inspects the GPU model, memory, driver, and catalog qualification, selects a conservative context tier, and starts the isolated CUDA 12.9 vLLM runtime after the model is ready.
Review the [GPU compatibility matrix](../reference/gpu-compatibility.md) before explicitly evaluating a configuration that has not completed qualification.

## Image Tags

Use immutable release tags for research work:

- `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10` — standard AMD64/ARM64 image;
- `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10-gpu-nvidia` — NVIDIA GPU image;
- `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10-terra` — Terra CPU image; and
- `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.10-terra-gpu-nvidia` — Terra NVIDIA image.

The moving `edge` tags represent current `main` and are intended for development, not reproducible analyses.
Release publication verifies candidate digests and manifest shape before creating version tags.

## Controlled Deployments

Use platform secrets, mounted credential files, or an approved identity mechanism rather than baking tokens into an image or command.
Add read-only mounts, network controls, user isolation, and resource limits according to the deployment threat model.

The container is packaging, not an authorization or compliance boundary by itself.
