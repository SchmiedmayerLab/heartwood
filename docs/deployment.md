<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Deploy Heartwood

This guide is for platform operators and research-computing teams. A deployment must define the application artifact, durable project storage, authorized model routes, security boundary, and support owner.

## Select the Artifact

| Environment | Artifact |
|---|---|
| General Docker host | Generic multi-platform image |
| Compatible NVIDIA Docker host | Explicit AMD64 NVIDIA image |
| Terra | Terra-derived portable or NVIDIA image |
| Stanford Carina | Native release installer |
| Another managed platform | No supported artifact; validate an integration before offering it |

Do not replace a platform-required base image or entrypoint with the generic image. Do not install Heartwood again inside a published Heartwood image.

## Provide Durable Project Storage

The whole project directory must survive process restart, container replacement, platform pause, and scheduler transitions. Heartwood stores configuration, sessions, downloaded models, Skills, logs, caches, and audit data under `<project>/.heartwood/`.

The deployment may mount model storage separately at `.heartwood/models/`, but users should still start every interface from the project and should not need workspace or state arguments.

A native installation root is application software, not project state. Place it outside the project boundary.

## Configure Model Access

Offer one or more routes that the institution has reviewed:

- a platform-managed model connection;
- OpenAI or Anthropic through an authorized account;
- an existing OpenAI-compatible service; or
- a local model served by the packaged CPU or NVIDIA runtime.

Images contain no model weights or credentials. Use an interactive prompt, platform secret, mounted credential file, or managed identity. Keep values out of image layers, build arguments, project configuration, logs, and examples.

Managed platform connections need explicit catalog and completion endpoints, credential bindings, and policy allowlists. Technical reachability is not a data-use approval.

## Establish the Security Boundary

Heartwood authorizes model routes, applies the selected OpenHands confirmation mode, confines its own file operations to the project, and produces a scrubbed audit export.

The deployment remains responsible for:

- operating-system and process isolation;
- user identity and storage permissions;
- network and egress enforcement;
- model-provider agreements and retention;
- dataset access and export rules;
- backup, monitoring, incident response, and support.

Agent terminal tools run with the Heartwood process's operating-system permissions. Environment-variable filtering reduces accidental credential exposure but is not hard process isolation.

## Validate the Deployment

Before offering Heartwood:

1. verify the exact immutable artifact and architecture;
2. start it under the intended user and persistent mount;
3. confirm the supported terminal, browser, notebook, and proxy paths;
4. validate every authorized model connection without exposing credentials;
5. exercise allow and reject decisions with synthetic files;
6. restart the process and confirm project and session recovery;
7. inspect a scrubbed audit export; and
8. document the support contact and approved data uses.

Repository tests validate software contracts with synthetic fixtures. They do not provide institutional approval for a deployment.

See [Supported Environments](platform-support.md), [Security and Data Boundaries](../design/05-security-compliance.md), and [Build a Platform-Specific Image](platform-images.md).
