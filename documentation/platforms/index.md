<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose a Platform

Run Heartwood where the project files are already stored and governed.
The platform determines installation, durable storage, available interfaces, credentials, Heartwood-managed inference, and which model connections are allowed; it does not change how Heartwood treats the current folder as a project.

## Supported Paths

| Platform | Installation | Terminal | Browser | Notebook | Models Run by Heartwood |
|---|---|---:|---:|---:|---|
| Workstation | Standard container | Yes | Yes | Not in the standard image | CPU inference |
| NVIDIA workstation/server | GPU container | Yes | Yes | Not in the GPU image | NVIDIA GPU inference |
| Linux without containers | Native release installer | Yes | Yes | Yes, in an existing Jupyter server | Host-dependent |
| Terra | Terra or Terra GPU image | Yes | No | Yes | CPU or NVIDIA GPU inference |
| Stanford Carina | Native release installer | Yes | No | No | NVIDIA GPU inference through requested compute |

The generic standard image is a multi-platform Linux image for AMD64 and ARM64.
GPU images are AMD64 because the pinned NVIDIA/vLLM stack is validated there.
Terra images are AMD64 single-platform Docker manifests because Terra image auto-detection requires that shape.

## Choose the Simplest Route

- Use the [standard container](containers.md) for the shortest workstation setup.
- Use the [native Linux installer](native-linux.md) when Docker is unavailable but `uv` and a compatible host environment are available.
- Use the [Terra image](terra.md) when the project is in a Terra workspace; it preserves Terra's Jupyter contract.
- Use the [native installer](carina.md) on Stanford Carina; containers are not the normal interactive path there.
- Follow [Add a Platform](../operate/platform-integration.md) when deploying into another managed environment.

Every path uses the process current directory as the project and `.heartwood/` inside that project for private state.
