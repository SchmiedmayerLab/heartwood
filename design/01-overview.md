<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Product and Scope

This section explains Heartwood's technical design for developers, operators, and reviewers. Researchers should begin with [Get Started](../docs/getting-started.md).

## Purpose

Heartwood brings a conversational coding agent into the computing environment where biomedical research files already reside. It adds explicit model routing, grouped action review, biomedical Skills, persistent sessions, and content-minimized audit records around an OpenHands agent.

The platform remains the authoritative boundary for identity, storage, networking, and data access. Heartwood does not replace a platform sandbox, institutional approval, statistical review, or clinical review.

## Product Principles

1. **Run with the project.** Keep agent execution and project state in the approved research environment.
2. **Use one interaction contract.** Present the same session through terminal, browser, and notebook adapters.
3. **Make decisions inspectable.** Preserve model-route decisions, proposed action groups, human decisions, tool outcomes, and Skill identity.
4. **Keep models deployment-owned.** Store neither model weights nor credential values in published images.
5. **Reuse upstream systems.** Use OpenHands for agent execution and tools, LiteLLM for model-provider compatibility, and platform infrastructure for identity, storage, and proxying.
6. **Keep platform differences at boundaries.** Isolate detection, policy, scheduler, image, storage, and proxy behavior behind adapters.

## Current Capabilities

Heartwood provides:

- an OpenHands-backed coding conversation with terminal and file tools;
- full-screen and line-oriented terminal interfaces;
- a browser application and notebook bridge over the same gateway;
- model connections for local services, OpenAI, Anthropic, Stanford AI API Gateway, and operator-configured compatible endpoints;
- public Hugging Face model inspection, download, verification, and managed CPU or NVIDIA launch where the artifact includes a runtime;
- grouped action confirmation with always-confirm and policy-controlled low-risk automatic approval;
- bundled synthetic biomedical Skills and explicit project extension installation;
- current-directory project state, replay, and scrubbed audit export;
- generic AMD64 and ARM64 images, explicit NVIDIA images, Terra-derived images, and a Carina native installation.

## Current Limits

- The built-in data-source adapter and reference workflow use synthetic OMOP-like fixtures. Heartwood does not discover, authorize, or validate a real biomedical dataset.
- Repository-verified Skills and local model recommendations are integration assets, not biomedical, statistical, clinical, production, or institutional certifications.
- OpenHands action groups receive one allow or reject decision. Heartwood does not present selective execution that the backend cannot honor.
- The notebook bridge does not supervise local models or manage Skills.
- Terminal tools retain the operating-system permissions of the Heartwood process.
- One process may write a session at a time.
- No Heartwood artifact or successful connection establishes approval for protected or controlled data.

## Users

- **Researchers** use the terminal, browser, or notebook to request and review work.
- **Methods developers** create Skills, fixtures, and reproducible evaluation workflows.
- **Platform operators** provide artifacts, storage, model routes, credentials, policy, and isolation.
- **Institutional reviewers** assess the exact deployment, data use, model route, evidence, and operational ownership.

## Reference Workflow

The repository exercises cohort creation, aggregate quality checks, a training-only baseline, and count-floor-controlled export against a small synthetic OMOP-like CSV fixture. The workflow verifies interface, Skill, action, persistence, and audit integration. It is not a live OMOP, BigQuery, biomedical-validity, or controlled-data claim.

Continue with [System Architecture](03-architecture.md), then use the topic-specific pages for deployment, security, Skills, audit, and testing.
