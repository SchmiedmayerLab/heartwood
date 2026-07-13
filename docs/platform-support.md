<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform Support

This matrix records current repository implementation and validation status. Release `0.1.0` establishes immutable distribution artifacts but does not complete live platform validation or confer institutional approval. Platform rationale and target-environment analysis belong in [02 — Platforms](../design/02-platforms.md); unimplemented work and release gates belong in the [Delivery Roadmap](../design/09-implementation-plan.md).

## Support Matrix

| Platform path | Repository status | Published image | Automated evidence | Live-platform status |
|---|---|---|---|---|
| Generic Linux or Jupyter environment | Implemented | `0.1.0`, `edge`, and `sha-<git-sha>` for `linux/amd64` and `linux/arm64` | Native architecture builds; exact-digest no-weight, real OpenHands loopback, action-confirmation, mounted llama.cpp, and fresh named-volume checks before tag promotion; live-browser cohort, baseline, aggregate-export, audit, and CLI replay; responsive Chromium and notebook-proxy contracts | Self-hosted deployments must validate their own identity, storage, network, and data controls. |
| Terra Jupyter | Implemented platform-derived image | `0.1.0-terra`, `edge-terra`, and `sha-<git-sha>-terra` for `linux/amd64` as Docker schema 2 | Real pinned Terra base on main; exact-digest Jupyter environment, Heartwood kernel, entrypoint, `/notebooks/...` route, Leonardo-compatible manifest, OpenHands reference cohort, mounted llama.cpp, CLI, web, notebook proxy, and audit checks before tag promotion | Real Terra workspace validation remains required before a supported or institution-approved deployment claim. |
| Stanford Carina CLI | Implemented native installation and launch contracts | Verified native release bundle; no Carina-specific image is published; `edge-gpu-nvidia` is only the equivalent generic container target | Deterministic platform detection, release-bundle verification, locked isolated environments, exact model-snapshot verification, explicit Slurm consent, shared runtime supervision, Stanford gateway manifest, and both OpenHands confirmation modes | A real GPU allocation, reviewed model, and content-free synthetic acceptance remain required before live-validated status. No controlled-data or institutional-approval claim. |
| All of Us or AnVIL through Terra | Design target only | No separately validated image | No platform-specific live evidence | Not currently supported as a distinct deployment. Dataset policy, identity, image base, and live control-plane behavior require separate validation. |
| Seven Bridges or Velsera | Design target only | None | None | Not currently supported. |
| DNAnexus or UK Biobank Research Analysis Platform | Design target only | None | None | Not currently supported. |

## Current Image Contracts

The generic and Terra images contain the same Heartwood application, OpenHands SDK adapter, model-connection catalog, repository-verified Skills, CLI, notebook bridge, web UI, policy layer, audit implementation, and optional CPU llama.cpp runtime. They contain no model weights or provider credentials. Both interfaces discover and select local, cloud, custom, and platform-provided research models through the same gateway contract; platform connections are deployment configuration rather than image variants.

Explicit `edge-gpu-nvidia` and `edge-terra-gpu-nvidia` variants add an isolated pinned vLLM environment without changing the Heartwood payload or embedding weights. The portable tags remain the default. GPU execution requires compatible NVIDIA drivers and separate native or live-platform validation.

The Terra image is an implemented packaging and Jupyter integration target, not a complete Terra runtime adapter. Unless a deployment supplies an explicit policy and injected adapters, session construction uses `GenericPlatformAdapter` and the synthetic OMOP data-source fixture. Real Terra policy, identity, workspace-data detection, and OMOP access remain delivery requirements.

Session and audit state are file-backed. Sequential CLI, notebook, and web access to the same workspace is implemented; concurrent independent processes writing the same session are not a supported deployment pattern until the single-writer gateway and recovery gate in the roadmap is complete.

The Terra image currently extends the pinned `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6` base declared in `images/platforms.toml`. Terra's official image catalog still lists that version, while Terra also offers the newer slim `terra-base:1.0.0`; migration to a different base is future work until the complete Jupyter, Leonardo, user, storage, proxy, and publication contract passes again.

## Support Claim Boundary

Repository continuous integration demonstrates software integration with synthetic fixtures. It does not establish a business associate agreement, Health Insurance Portability and Accountability Act eligibility, dataset authorization, private networking, identity binding, retention policy, clinical validity, or institutional approval.

A platform moves from CI-validated to live-validated only after the published immutable image passes the documented synthetic workflow in the real control plane, including startup, proxy routing, persistent storage, autopause and resume, model-catalog discovery, profile authorization, action confirmation, Skills, replay, and scrubbed audit export. The [Terra Jupyter Demo](terra-jupyter-demo.md) defines that evidence for Terra.

## Authoritative Platform References

- [Terra custom cloud environment tutorial](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks)
- [Terra cloud environment customization](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app)
- [Terra architecture and persistent-disk mounts](https://support.terra.bio/hc/en-us/articles/360058163311-Terra-architecture-where-your-data-and-tools-live)
- [Accessing workspace-bucket data from a notebook](https://support.terra.bio/hc/en-us/articles/360046617372-Accessing-data-from-the-workspace-Bucket-in-a-notebook)
- [DataBiosphere Terra Docker image catalog](https://github.com/DataBiosphere/terra-docker)
