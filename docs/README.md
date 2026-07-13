<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Documentation

Heartwood documentation is organized by purpose so current behavior, durable design rationale, and planned implementation cannot be mistaken for one another.

## Current Operational Documentation

These documents describe commands, images, interfaces, and validation paths implemented in the repository:

- [Platform Support](platform-support.md) records the current support and validation status for each platform.
- [Container Images](container-images.md) defines published tags, image contents, runtime configuration, and continuous integration.
- [Releases](releases.md) defines Semantic Versioning, automated release gates, approval, and published artifacts.
- [Model Connections](model-connections.md) defines built-in, platform-provided, local, cloud, and custom model configuration.
- [Researcher Web Interface](web-interface.md) documents the shared session workflow, model setup, action review, audit and CLI parity, notebook layout, and reproducible screenshots.
- [Using Heartwood](using-heartwood.md) documents the shared conversation, action-set review, terminal commands, replay, audit, web, notebook, and persistence behavior.
- [Set Up Heartwood On Carina](carina-cli.md) documents the synthetic-only native GPU and Stanford AI API Gateway workflow.
- [Getting Started With Local And Offline Models](getting-started-offline.md) is the runnable generic-container and local-inference guide.
- [Terra Jupyter Demo](terra-jupyter-demo.md) is the synthetic Terra validation runbook.
- [Platform Image Extension Guide](platform-images.md) documents the implemented shared mechanism for maintaining or adding a platform-derived image.

Operational documentation describes repository behavior, not institutional approval. A path marked implemented or CI-validated is not automatically approved for controlled data.

## Design And Rationale

The [design set](../design/01-overview.md) owns durable product and engineering decisions:

- [Overview](../design/01-overview.md) defines scope, users, and the reference workflow.
- [Platforms](../design/02-platforms.md) explains platform assumptions and target-environment rationale.
- [Architecture](../design/03-architecture.md) defines component ownership and runtime data flow.
- [Skills And Auto-Detection](../design/04-skills.md) defines Skill packaging, trust, detection, and extension behavior.
- [Security And Compliance](../design/05-security-compliance.md) defines threat boundaries and evidence requirements.
- [Observability, Audit, And Feedback](../design/06-observability-audit.md) defines records, replay, and export behavior.
- [Testing And Evaluation](../design/07-testing-eval.md) defines implemented test layers and evaluation requirements.
- [Development Practices](../design/08-development.md) defines repository, toolchain, continuous-integration, and supply-chain policy.

Design documents explain why the system is shaped this way. Runnable commands and platform status belong in the operational documents instead.

## Project Planning

[GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) own planned implementation, acceptance criteria, and dependencies; the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) owns delivery status. Project documentation does not serve as a backlog or implementation diary. When a planned change alters a durable product, architecture, security, or testing decision, update the owning design document as part of that change.

## Published Documentation

The public [Heartwood documentation site](https://schmiedmayerlab.github.io/heartwood/) is built from the canonical `README.md`, `docs/`, `design/01` through `design/08`, and `ACRONYMS.md`; there is no second editable documentation copy. Pull requests and `main` build that source strictly, while the public site changes only when the protected release workflow publishes an existing Semantic Version tag. A manual recovery deployment accepts only the current latest published release, so continued documentation work on `main` cannot alter or roll back the public release snapshot.

Preview the current source locally:

```bash
uv sync --locked --only-group docs
uv run --no-sync python deploy/stage_documentation.py
uv run --no-sync zensical serve
```

Use `uv run --no-sync zensical build --clean --strict` to run the same strict static build as CI.

## Status Terms

| Term | Meaning |
|---|---|
| Implemented | Code and repository tests exist for the behavior. |
| CI-validated | Automated checks exercise the behavior in synthetic or controlled test infrastructure. |
| Live-validated | The published artifact has passed the documented synthetic workflow in the real platform control plane. |
| Institution-approved | The deploying institution has approved the exact image, model route, credentials, data use, network controls, and evidence. Heartwood cannot assign this status. |
| Release-ready | Every acceptance gate for the declared release scope is reproducible from an immutable published artifact. |
| Planned | The item is future work and must not be described as current capability. |
