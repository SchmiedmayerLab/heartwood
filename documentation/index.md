<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

Heartwood is an open-source research agent system for biomedical research environments.
You describe work on a project folder in ordinary language; an OpenHands agent proposes commands and file changes; the Heartwood gateway decides which proposals need you, records every decision, and keeps a verifiable audit history.

[Start Your First Project](start/index.md){ .md-button .md-button--primary }
[Choose a Platform](platforms/index.md){ .md-button }

## Components

- The gateway owns project state, policy, action review, authorization of operations outside the agent, session records and audit.
- The agent loop and coding tools come from OpenHands and run either in the Heartwood process or as a complete worker inside an OpenShell sandbox.
- The terminal, browser and notebook interfaces present the same gateway contract, so a session, its settings and its review decisions look the same in each.
- Research Skills, bounded research specialists and staged research workflows run inside the same session, review and records.

## Roles and Responsibilities

| Role | Decides | Starts with |
|---|---|---|
| Researcher | The task, the model where a choice is offered, and every proposal that waits for review | [Your First Project](start/index.md) |
| Operator | How Heartwood runs: the deployment manifest, protected state, review-mode ceiling, worker profile, credentials and checkpoint signer | [Deploy Heartwood](operate/index.md) |
| Platform | Identity, storage, network controls, compute and inference, under its own authority | [Platform Contract](operate/platform-contract.md) |

## Two Deployment Shapes

### Local Launch

Run `heartwood` in a project folder.
The current directory is the project, and Heartwood keeps configuration, sessions, models and audit records in `.heartwood/` inside it.
Every action set waits for you; **Review Changes** can be selected for the running process when the model route allows it, as described in [Review Modes](use/actions-audit.md#review-modes).

Agent tools run with your operating-system permissions.
An allowed terminal command can read anything you can read and can change `.heartwood/`, including session and audit files, so a local launch is a reviewed assistant rather than an isolation boundary.
This is the shape used on workstations, in containers, on Terra and on Stanford Carina.

### Operator-Bound Deployment

An operator starts the gateway with `heartwood gateway serve --deployment` and a manifest that binds the project to protected state outside the research folder and sets the policy and review-mode ceiling.
With a `[worker]` section, each session runs in a sandboxed OpenShell worker on an operator-controlled Linux Docker host.
The worker receives selected read-only inputs and verified Skills; protected state, provider credentials, the Docker socket and signing keys stay outside it.
Operations outside the worker, and the release of their results to it, are authorized only by a researcher decision bound to the exact content reviewed, in every review mode.
No production outside-service adapter ships and `gateway serve` installs none; these operations are available only through an embedding launcher.
See [Projects and Private State](start/project.md#operator-bound-external-state) and [Security and Controlled Data](operate/security.md).

## Support Levels

Container, GPU container and Terra images and native installers are built and tested in CI for each release commit.
The isolated worker passes native qualification in CI on Linux Docker with synthetic workloads, and the evidence is bound to the commit.
Heartwood-managed model configurations are qualified per platform as listed in [GPU Compatibility](reference/gpu-compatibility.md).
[Support and Compatibility](operate/support.md#compatibility) gives the level of each configuration, and [Security Limits](operate/security.md#security-limits) states what the controls do not cover.
Use synthetic or non-sensitive files until the institution has approved the deployment for a data class.

## Find an Answer

- [Your First Project](start/index.md) goes from an empty folder to a reviewed action.
- [Work With Heartwood](use/index.md) covers conversations, the interfaces, Skills, specialists and research workflows.
- [Actions and Audit History](use/actions-audit.md) explains review modes, outside operations, replay, exports and signed checkpoints.
- [Models](models/index.md) compares institution-managed, hosted, compatible-service and Heartwood-managed routes.
- [Platforms](platforms/index.md) covers containers, native Linux, Terra and Stanford Carina.
- [Diagnostics and Troubleshooting](reference/troubleshooting.md) maps `HW-*` codes to recovery steps.
- [Architecture](architecture/index.md) explains the components and their boundaries.
