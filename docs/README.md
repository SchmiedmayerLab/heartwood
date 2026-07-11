<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Documentation

Heartwood documentation is organized by purpose so implemented behavior, design rationale, and future work cannot be mistaken for one another.

## Current Operational Documentation

These documents describe commands, images, interfaces, and validation paths implemented in the repository:

- [Platform Support](platform-support.md) records the current support and validation status for each platform.
- [Container Images](container-images.md) defines published tags, image contents, runtime configuration, and continuous integration.
- [Model Connections](model-connections.md) defines built-in, platform-provided, local, cloud, and custom model configuration.
- [Getting Started With Local And Offline Models](getting-started-offline.md) is the runnable generic-container and local-inference guide.
- [Terra Jupyter Demo](terra-jupyter-demo.md) is the synthetic Terra validation runbook.
- [Platform Image Extension Guide](platform-images.md) documents the implemented shared mechanism for maintaining or adding a platform-derived image.

Operational documentation describes repository behavior, not institutional approval. A path marked implemented or CI-validated is not automatically approved for controlled data.

## Design And Rationale

The [design set](../design) owns durable product and engineering decisions:

- [Overview](../design/01-overview.md) defines scope, users, and the reference workflow.
- [Platforms](../design/02-platforms.md) explains platform assumptions and target-environment rationale.
- [Architecture](../design/03-architecture.md) defines component ownership and runtime data flow.
- [Skills And Auto-Detection](../design/04-skills.md) defines Skill packaging, trust, detection, and extension behavior.
- [Security And Compliance](../design/05-security-compliance.md) defines threat boundaries and evidence requirements.
- [Observability, Audit, And Feedback](../design/06-observability-audit.md) defines records, replay, and export behavior.
- [Testing And Evaluation](../design/07-testing-eval.md) defines implemented test layers and evaluation requirements.
- [Development Practices](../design/08-development.md) defines repository, toolchain, continuous-integration, and supply-chain policy.

Design documents explain why the system is shaped this way. Runnable commands and platform status belong in the operational documents instead.

## Delivery Roadmap

The [Delivery Roadmap](../design/09-implementation-plan.md) records the test-backed baseline, material readiness gaps, ordered delivery priorities, acceptance gates, and deferred work. Operational issue tracking may reference roadmap items, but it does not replace the architecture and acceptance criteria in the design record.

## Status Terms

| Term | Meaning |
|---|---|
| Implemented | Code and repository tests exist for the behavior. |
| CI-validated | Automated checks exercise the behavior in synthetic or controlled test infrastructure. |
| Live-validated | The published artifact has passed the documented synthetic workflow in the real platform control plane. |
| Institution-approved | The deploying institution has approved the exact image, model route, credentials, data use, network controls, and evidence. Heartwood cannot assign this status. |
| Release-ready | Every acceptance gate for the declared release scope is reproducible from an immutable published artifact. |
| Planned | The item is future work and must not be described as current capability. |
