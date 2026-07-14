<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Choose Where to Run Heartwood

Heartwood should run in the same approved computing environment as the files it may inspect and modify. The interaction stays consistent across environments: start Heartwood from the project directory, choose an authorized model, and use the terminal, browser, or notebook support available there.

## Choose an Environment

| Situation | Recommended setup | Main interface |
|---|---|---|
| Learning Heartwood on a workstation | Generic Heartwood container | Browser or terminal |
| Working on a general Linux server | Generic container or native installation, according to local policy | Browser or terminal |
| Working in Terra | Terra-derived Heartwood image | Browser through Jupyter, terminal, or notebook |
| Working on Stanford Carina | Native Heartwood installation | Interactive terminal |
| Working on another managed research platform | Operator-reviewed generic or platform-derived deployment | Depends on platform routing |

The generic container is the easiest local starting point because it includes the complete application and a supported CPU inference runtime. A native installation is better when the environment already controls software, scheduling, and storage. A platform-derived image is necessary when replacing the platform's base image would break its notebook, identity, or routing contract.

## Keep One Project Model

The project is always the directory where the Heartwood process starts:

- In a native installation, change into the analysis directory before running `heartwood`.
- In the generic container, mount the analysis directory at `/workspace`, which is the image's starting directory.
- In Terra, create or open an analysis directory on the persistent `/home/jupyter` disk and start Heartwood there.
- In Carina, enter a project directory on approved storage before Heartwood requests compute.

Heartwood creates `.heartwood/` inside that project. Platform home directories and container mount points provide persistent storage; they do not create a different Heartwood workspace concept.

## Understand Platform Responsibilities

Heartwood can detect supported platform signals and apply a conservative model-route and action policy. It does not decide whether a machine, model provider, or dataset is institutionally approved.

Before using controlled data, the deployment owner must verify:

- user identity and project access;
- persistent storage and backup behavior;
- network routes and egress controls;
- model-provider agreements and service settings;
- dataset permissions and export rules;
- the exact Heartwood artifact and platform integration.

## Follow a Platform Guide

- [Heartwood on Terra](terra-jupyter-demo.md) explains the custom image, persistent project directory, Jupyter proxy, model options, and synthetic validation workflow.
- [Heartwood on Stanford Carina](carina-cli.md) explains private storage, installation, local GPU inference, scheduler consent, and synthetic validation.
- [Run Heartwood in a Container](container-images.md) covers the generic image for workstations and self-hosted environments.

[Platform Support and Validation](platform-support.md) provides the release-specific evidence matrix. It deliberately separates implemented software from automated validation, real-platform validation, and institutional approval.
