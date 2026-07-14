<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform Support

This matrix records the implementation and validation evidence available for release `0.2.0`. A repository feature, automated test, or published image is not evidence of institutional approval. Platform rationale and deployment assumptions are documented in [Platforms](../design/02-platforms.md).

## Support Matrix

| Platform path | Repository status | Published image | Automated evidence | Live-platform status |
|---|---|---|---|---|
| Generic Linux or Jupyter environment | Implemented | `0.2.0`, `edge`, and `sha-<git-sha>` for `linux/amd64` and `linux/arm64` | Native architecture builds; exact-digest no-weight, real OpenHands loopback, grouped action confirmation, mounted llama.cpp, one-project-volume recovery, browser workflow, audit, CLI replay, responsive Chromium, and notebook-proxy contracts | Self-hosted deployments must validate their own identity, storage, network, and data controls. |
| Terra Jupyter | Implemented platform-derived image | `0.2.0-terra`, `edge-terra`, and `sha-<git-sha>-terra` for `linux/amd64` as Docker schema 2 | Real pinned Terra base on `main`; exact-digest Jupyter environment, Heartwood kernel, inherited entrypoint, `/notebooks/...` route, Leonardo-compatible manifest, current-directory project persistence, OpenHands reference workflow, mounted llama.cpp, CLI, web, notebook proxy, and audit checks before promotion | Real Terra workspace validation remains required before a supported or institution-approved deployment claim. |
| Stanford Carina CLI | Implemented native installation and launch contracts | `0.2.0` native release bundle; no Carina-specific image is published | Release-bundle and installer-layout verification; process-level Slurm handoff; exact project-directory preservation; runtime supervision, setup, session, shutdown, and scratch cleanup; Micromamba activation; pinned FFmpeg bootstrap; locked Heartwood and vLLM environments; verified model download; GPU-partition discovery; explicit compute consent; grouped OpenHands action confirmation; narrow-terminal interaction; Stanford gateway connection; and both confirmation modes | Release `0.2.0` has not completed the documented synthetic workflow from its published artifact on Carina. No controlled-data or institutional-approval claim is made. |

## Current Image Contracts

The generic and Terra images contain the same Heartwood application, OpenHands SDK adapter, model-connection catalog, repository-verified Skills, CLI, notebook bridge, web UI, policy layer, audit implementation, and optional CPU llama.cpp runtime. They contain no model weights or provider credentials. Both interfaces discover and select local, cloud, custom, and platform-provided research models through the same gateway contract; platform connections are deployment configuration rather than image variants.

Explicit `edge-gpu-nvidia` and `edge-terra-gpu-nvidia` variants add an isolated pinned vLLM environment without changing the Heartwood payload or embedding weights. The portable tags remain the default. GPU execution requires compatible NVIDIA drivers and separate native or live-platform validation.

The Terra image is an implemented packaging and Jupyter integration target. It detects the Terra process environment and uses the conservative Terra route policy, but it does not infer authorization for workspace data, BigQuery, or a hosted model. A deployment must provide and validate those platform-specific identities, routes, and data adapters before making a broader support claim.

Session and audit state are file-backed under the current project's `.heartwood/` directory. Sequential CLI, notebook, and web access to the same project is implemented; concurrent independent processes writing the same session are not supported.

The Terra image extends the pinned `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6` base declared in `images/platforms.toml`. No Heartwood support claim applies to another Terra base unless its Jupyter, Leonardo, user, storage, proxy, and publication contracts have been validated independently.

## Support Claim Boundary

Repository continuous integration demonstrates software integration with synthetic fixtures. It does not establish a business associate agreement, Health Insurance Portability and Accountability Act eligibility, dataset authorization, private networking, identity binding, retention policy, clinical validity, or institutional approval.

A platform moves from CI-validated to live-validated only after the published immutable image passes the documented synthetic workflow in the real control plane, including startup, proxy routing, persistent storage, autopause and resume, model-catalog discovery, profile authorization, action confirmation, Skills, replay, and scrubbed audit export. The [Terra Jupyter Demo](terra-jupyter-demo.md) defines that evidence for Terra.

## Authoritative Platform References

- [Terra custom cloud environment tutorial](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks)
- [Terra cloud environment customization](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app)
- [Terra architecture and persistent-disk mounts](https://support.terra.bio/hc/en-us/articles/360058163311-Terra-architecture-where-your-data-and-tools-live)
- [Accessing workspace-bucket data from a notebook](https://support.terra.bio/hc/en-us/articles/360046617372-Accessing-data-from-the-workspace-Bucket-in-a-notebook)
- [DataBiosphere Terra Docker image catalog](https://github.com/DataBiosphere/terra-docker)
