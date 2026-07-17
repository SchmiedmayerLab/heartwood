<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform Support and Validation

This page records the evidence available for release `0.2.0-beta.3`. Start with [Choose Where to Run Heartwood](platforms.md) when deciding which setup to use; this page is the detailed reference for operators and reviewers.

An implemented feature, automated test, or published artifact is not evidence of institutional approval. The [Documentation Guide](README.md#documentation-status) defines each status term, and [Platform Architecture](../design/02-platforms.md) explains the underlying deployment assumptions.

!!! info "How to read this page"

    Use **Current Status** for the release-level claim, then read the matching platform section for the exact automated and live evidence. The final institutional decision always belongs to the deploying organization.

## Current Status

| Platform path | Available artifact | Current claim | Required deployment work |
|---|---|---|---|
| Generic Linux or Jupyter environment | Generic release container for AMD64 and ARM64 | Implemented and CI-validated | Validate the host's identity, storage, network, model, and data controls |
| Terra Jupyter | Terra-derived release image for AMD64 | Implemented, CI-validated, and partially live-validated with synthetic data | Complete one immutable-image model interaction and grouped decision workflow, then obtain any required institutional review |
| Stanford Carina | Native release installer and bundle | Implemented and CI-validated | Complete the synthetic workflow from the published release on Carina, then obtain any required institutional review |

No listed path is automatically approved for protected health information or another controlled dataset.

## Validation Evidence

### Generic Container

The published tags are `0.2.0-beta.3`, `edge`, and immutable `sha-<git-sha>` tags. Continuous integration builds native AMD64 and ARM64 artifacts and checks the no-weight image contract, centrally recommended and user-selected model planning, OpenHands loopback conversation, grouped action confirmation, mounted llama.cpp inference, one-project-volume recovery, browser workflow, audit export, CLI replay, responsive browser layout, and notebook-proxy behavior.

These checks use synthetic fixtures. A self-hosted deployment must still validate the exact host, identity, storage, network, provider, model, and data-use controls.

### Terra

The published tags are `0.2.0-beta.3-terra`, `edge-terra`, and immutable `sha-<git-sha>-terra` tags. Terra tags use the single-platform Docker schema-2 format required by Leonardo.

Continuous integration builds from the real pinned Terra base on `main` and checks the Jupyter environment, Heartwood kernel, inherited entrypoint, notebook route, Leonardo-compatible manifest, dedicated project placement under persistent storage, restart persistence, exact authenticated browser routing through Jupyter Server Proxy, portable and NVIDIA runtime metadata, deployment-aware model recommendations, the shared local-model contract, OpenHands synthetic workflow, mounted llama.cpp inference, secured vLLM configuration loading, CLI, notebook bridge, and audit export.

Real Terra workspace validation remains required before a supported or institution-approved deployment claim. Synthetic live validation has confirmed the Terra base image, Heartwood kernel, current-directory project, persistent `.heartwood/` state across pause and resume, local-model planning and download, NVIDIA T4 discovery, and the authenticated Leonardo proxy path. A complete model interaction, grouped action decision, replay, and audit workflow has not yet passed on one immutable Terra image. The image detects Terra and applies exact allowlists for its built-in model routes, but it does not infer authorization for workspace data, BigQuery, a hosted model, or exports.

### Stanford Carina

Release `0.2.0-beta.3` provides the native installation bundle; no Carina-specific container image is published. Continuous integration verifies the installer layout, locked Heartwood and vLLM environments, Micromamba bootstrap, vLLM and PyTorch runtime imports, Slurm handoff, exact current-directory project preservation, runtime supervision, setup, session lifecycle, shutdown, scratch cleanup, recommended and user-selected model planning, verified download, GPU-partition discovery, explicit compute consent, grouped OpenHands confirmation, narrow-terminal interaction, Stanford AI API Gateway connection, and both permitted confirmation modes.

Live synthetic platform testing covered the complete beta.3 workflow on one NVIDIA L40S GPU. Installation started in the selected project-storage directory without external path, cache, home, or version inputs; completed all seven stages in 893 seconds; removed transient installer state; kept both application environments on the persistent bootstrap interpreter; and bound Carina into the installed launcher. The pinned Qwen2.5 7B snapshot staged to job-local scratch, vLLM became ready in 124 seconds, and the in-allocation diagnostic passed its model, project, policy, scratch, scheduler, and GPU checks. The session invoked a repository-verified synthetic cohort Skill, displayed exact structured arguments before review and in replay, allowed one bounded terminal action, rejected file actions without creating them, excluded an invalid proposal, paused and resumed an idle session without another model call, verified a content-minimized hash-chained audit export while retaining private diagnostics, exited normally, and preserved 16 unrelated normal-QoS tasks. The tested 7B model also produced unacceptable free-form narrative drafts, so deterministic Skill results and successful tool execution are not evidence of research validity. Carina remains implemented and CI-validated until the immutable beta.3 artifact repeats the live workflow; the supported presentation is the terminal, and no authenticated browser-proxy route, controlled-data approval, or research-validity claim is made.

## Shared Image Contract

The generic and Terra images contain the same Heartwood application, OpenHands SDK adapter, model connections, local recommendations, Hugging Face planner, repository-verified Skills, CLI, notebook bridge, browser interface, policy layer, and audit implementation. The portable images also contain the supported CPU llama.cpp runtime. They contain no model weights or provider credentials.

Explicit `edge-gpu-nvidia` and `edge-terra-gpu-nvidia` variants add a pinned CUDA 11.8 vLLM environment without changing the Heartwood application or embedding weights. The portable tags remain the default. GPU execution requires a driver compatible with CUDA 11.8, a suitable model, enough accelerator memory, and deployment-specific validation. The secured launcher verifies the model-configuration security backport, checks CUDA initialization, and reports the selected context together with conservative RAM and GPU-memory guidance before starting vLLM.

Session and audit state remain under the current project's `.heartwood/` directory. Sequential CLI, notebook, and browser access to one project is implemented; concurrent independent processes writing the same session are not supported.

The Terra image extends `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6`. No support claim applies to another Terra base until its Jupyter, Leonardo, user, storage, proxy, and publication contracts have been validated independently.

## Support Claim Boundary

Repository continuous integration demonstrates software integration with synthetic fixtures. It does not establish a business associate agreement, Health Insurance Portability and Accountability Act eligibility, dataset authorization, private networking, identity binding, retention policy, clinical validity, or institutional approval.

A path becomes live-validated only after an immutable published artifact completes the documented synthetic workflow in the real control plane, including startup, routing, persistent storage, pause and resume, model discovery, profile authorization, action confirmation, Skills, replay, and scrubbed audit export. The [Terra guide](terra-jupyter-demo.md) and [Carina guide](carina-cli.md) define the current platform workflows.

## Authoritative Platform References

- [Terra custom cloud environment tutorial](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks)
- [Terra cloud environment customization](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app)
- [Terra architecture and persistent-disk mounts](https://support.terra.bio/hc/en-us/articles/360058163311-Terra-architecture-where-your-data-and-tools-live)
- [Accessing workspace-bucket data from a notebook](https://support.terra.bio/hc/en-us/articles/360046617372-Accessing-data-from-the-workspace-Bucket-in-a-notebook)
- [DataBiosphere Terra Docker image catalog](https://github.com/DataBiosphere/terra-docker)

## Continue from Here

- Researchers should return to [Choose Where to Run Heartwood](platforms.md) and follow the matching platform guide.
- Operators should use [Deploy Heartwood](deployment.md) to map the artifact, storage, model route, security boundary, and validation evidence.
- Reviewers should continue with [Security and Compliance](../design/05-security-compliance.md) and [Testing and Evaluation](../design/07-testing-eval.md).
