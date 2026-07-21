<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

Heartwood is an open-source coding agent for biomedical research projects.
Describe the work you need in ordinary language, inspect the files and commands the agent proposes, and keep a reviewable history of the session inside your research environment.

Heartwood is designed for workstations, containers, Terra, Stanford Carina, and operator-managed research platforms.
It can use models provided by a research environment, hosted model services, another compatible service, or a model managed by Heartwood in the same compute environment.

[Start Your First Project](start/index.md){ .md-button .md-button--primary }
[Choose a Platform](platforms/index.md){ .md-button }

## What You Can Do

### Work Through a Research Task

Ask Heartwood to inspect code, prepare an analysis, explain a result, or update project files.
Heartwood treats the folder from which you start it as the project, so begin in a dedicated folder that contains only the material intended for the session.

### Review Actions Before They Run

Heartwood presents related proposed actions together before execution under the default **Ask Every Time** setting.
You can allow or reject the complete set after inspecting every proposed command and file operation.

### Reuse Research Skills

Repository-verified Skills provide reusable instructions and tools for supported research workflows.
Projects can add explicitly reviewed Skills for additional workflows.

### Continue Across Interfaces

The terminal, browser, and notebook bridge use the same project configuration, model selection, sessions, action decisions, and audit history when the platform supports those interfaces.

## Start With Your Environment

| Where You Work | Recommended Starting Point | Available Interfaces |
|---|---|---|
| macOS or Linux workstation | [Heartwood container](platforms/containers.md) | Terminal and browser; Jupyter not included |
| Existing Python development environment | [Native development install](start/install.md#development-installation) | Terminal, browser, notebook |
| Terra workspace | [Heartwood for Terra](platforms/terra.md) | Terminal and notebook |
| Stanford Carina | [Heartwood for Carina](platforms/carina.md) | Terminal |
| Managed research platform | [Deployment guidance](operate/index.md) | Defined by the platform deployment |

## Choose Where the Model Runs

Heartwood does not include model weights or credentials in its images or installers.
During setup, choose an available research-environment connection, OpenAI, Anthropic, another authorized OpenAI-compatible service, or a [model that Heartwood manages in the current environment](models/choose-managed.md).

The platform remains authoritative for identity, network access, data permissions, and which model routes may receive project content.

## Understand the Boundary

Heartwood can operate inside a reviewed environment, but installing it does not authorize access to protected health information, approve a model provider, or make a deployment compliant.
Your institution remains responsible for the research platform, agreements, access controls, storage, networking, and data handling rules.

Begin with synthetic or non-sensitive files until the complete deployment has been reviewed for the intended data.
See [Security and Controlled Data](operate/security.md) before using restricted data.

## Find an Answer

- [Your First Project](start/index.md) provides the shortest complete path from installation to a reviewed action.
- [Work With Heartwood](use/index.md) explains the normal conversation, action, replay, and audit workflow.
- [Models](models/index.md) compares research-environment, hosted, compatible-service, and Heartwood-managed routes.
- [Platforms](platforms/index.md) covers containers, Terra, and Stanford Carina.
- [Diagnostics and Troubleshooting](reference/troubleshooting.md) maps stable `HW-*` codes to recovery steps.
- [How Heartwood Works](architecture/index.md) explains the architecture and security boundaries.
