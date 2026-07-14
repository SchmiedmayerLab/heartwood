<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Deploy Heartwood

This section is for platform operators, research-computing teams, and reviewers preparing Heartwood for a specific environment. Researchers starting an existing deployment should use [Get Started](getting-started.md) or [Choose Where to Run Heartwood](platforms.md).

## Choose the Deployment Artifact

| Environment | Artifact | Why |
|---|---|---|
| Workstation or general Linux host | Generic container image | Complete portable application with AMD64 and ARM64 support |
| Compatible NVIDIA host | Explicit NVIDIA container image | Adds the packaged GPU inference environment without embedding model weights |
| Terra | Terra-derived image | Preserves Terra's Jupyter, user, storage, entrypoint, and Leonardo routing contract |
| Scheduler-managed host such as Carina | Native release bundle | Integrates with the host package manager, shared storage, and scheduler |
| Another managed notebook platform | Validated platform-derived image | Preserves platform behavior while adding the shared Heartwood payload |

Do not install Heartwood a second time inside a published Heartwood image. Do not use the generic image when the platform requires a specific base image or entrypoint.

## Provide Durable Project Storage

Heartwood keeps configuration, sessions, models, Skills, logs, caches, and audit data under `<project>/.heartwood/`. The deployment must make the whole project directory durable across process restarts, container replacement, platform pause, and scheduler transitions.

The deployment may mount model storage separately at `.heartwood/models/`, but the logical project layout must remain unchanged. On a fresh project, Heartwood recognizes an otherwise empty `.heartwood/` containing that regular model mount and initializes the remaining state around it without removing model files. Researchers should not need deployment-specific workspace, state, model, or cache arguments.

## Configure Model Access

Choose one or more model paths appropriate for the environment:

- a platform-managed research connection with a non-secret credential binding;
- a hosted provider authorized by exact endpoint and deployment policy;
- an existing OpenAI-compatible service;
- a project-local model served by the portable CPU or explicit NVIDIA runtime.

Images contain no model weights and no provider credentials. Supply credentials through a session-only prompt, platform secret, mounted credential file, or managed identity. Keep values out of image layers, labels, build arguments, project configuration, logs, and examples.

## Establish the Security Boundary

Heartwood authorizes model routes, configures OpenHands action confirmation, constrains its own file operations to the project, and produces a content-minimized audit export. The deployment remains responsible for operating-system isolation, user identity, storage permissions, network enforcement, provider contracts, data authorization, and export controls.

OpenHands terminal tools run with the permissions of the Heartwood process. Use a supported OpenHands remote workspace or platform-native sandbox when agent tools must be isolated from model credentials or files outside the project.

## Validate Before Use

Validation progresses through distinct claims:

1. **Implemented:** the repository contains the integration and tests.
2. **CI-validated:** automated checks exercise the artifact with synthetic data.
3. **Live-validated:** an immutable artifact passes the synthetic workflow in the real platform control plane.
4. **Institution-approved:** the deploying institution approves the exact artifact, model route, identity, data use, network controls, and evidence.

Do not infer a later claim from an earlier one. [Platform Support and Validation](platform-support.md) records the current evidence for each published path.

## Continue with the Technical Guides

- [Container Images](container-images.md) documents tags, mounts, local inference, runtime controls, and publication behavior.
- [Build a Platform-Specific Image](platform-images.md) documents the declarative platform-image extension contract.
- [Release Heartwood](releases.md) documents the protected release process.
- [Platform Architecture](../design/02-platforms.md) explains why platform-specific artifacts exist.
- [Security and Compliance](../design/05-security-compliance.md) defines the threat and governance boundaries.
