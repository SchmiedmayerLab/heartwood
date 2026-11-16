<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose a Platform

Run Heartwood where the project files are already stored and governed.
The platform determines installation, durable storage, available interfaces, credentials, Heartwood-managed inference and which model connections are allowed.
Each path on this page is a local launch with `.heartwood/` state in the project folder.

## Supported Paths

| Platform | Installation | Terminal | Browser | Notebook | Models Run by Heartwood |
|---|---|:-:|:-:|:-:|---|
| Workstation | Standard container | Yes | Yes | Not in the image | CPU inference |
| NVIDIA workstation or server | GPU container | Yes | Yes | Not in the image | NVIDIA GPU inference |
| Linux without containers | Native installer | Yes | Yes | Yes, through `heartwood-jupyter` | CPU inference |
| Terra | Terra or Terra GPU image | Yes | No | Yes | CPU or NVIDIA GPU inference |
| Stanford Carina | Native installer | Yes | No | No | NVIDIA GPU inference through Slurm |

The standard image is a multi-platform Linux image for AMD64 and ARM64.
GPU images are AMD64 because the pinned NVIDIA and vLLM stack is built for that architecture.
Terra images are AMD64 single-platform Docker manifests because Terra image auto-detection requires that shape.

## Qualification

| Platform | What the release establishes |
|---|---|
| Containers | Images built and smoke-tested in CI on AMD64 and ARM64, including offline and no-network checks; the CPU model `qwen25-7b-instruct-q4_k_m` is qualified for coding-agent use on this platform |
| Native Linux | Installer built and installed from an empty Ubuntu 24.04 AMD64 container in CI |
| Terra | Images built from the Terra base and tested in CI for Jupyter inheritance, proxy routing and persistence; one GPU model configuration was qualified live in a Terra workspace |
| Stanford Carina | Installer layout tested in CI; one GPU model configuration was qualified live on Carina |

Each platform adapter reports its validation level as `ci`.
The live model qualifications are listed with their dates and drivers in [GPU Compatibility](../reference/gpu-compatibility.md); they qualify that model configuration, not a deployment for research data.

## Choose the Simplest Route

- Use the [standard container](containers.md) for the shortest workstation setup.
- Use the [native Linux installer](native-linux.md) when Docker is unavailable but `uv` and a compatible host are.
- Use the [Terra image](terra.md) when the project is in a Terra workspace.
- Use the [Carina installer](carina.md) on Stanford Carina, where containers are not the interactive path.
- Follow [Add a Platform](../operate/platform-integration.md) to deploy into another managed environment.

## Isolated Worker Deployments

The isolated worker is an [operator-bound deployment](../start/project.md#operator-bound-external-state), not a platform image.
It runs on a Linux host with an operator-controlled Docker engine and a registered OpenShell gateway, and is qualified in CI with synthetic workloads only.
None of the platforms above runs it by installing an image; Terra and Carina launches use local state and the Heartwood process identity.
See [Security and Controlled Data](../operate/security.md) for what the worker boundary covers and what it does not establish.
