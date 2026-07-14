<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Documentation

Start with [Use Heartwood](using-heartwood.md). It explains the project-directory model and the interaction shared by the terminal, browser, and notebooks without requiring knowledge of the internal architecture.

## Get Started

- [Use Heartwood](using-heartwood.md) explains projects, setup, conversations, action review, resume, and audit export.
- [Choose a Model](model-connections.md) explains the choices shown during model setup and how credentials are handled.
- [Local and Offline Models](getting-started-offline.md) explains existing local services, recommended and user-selected Hugging Face downloads, automatic runtime selection, managed launch, and no-network validation.
- [Work with Heartwood in a Browser](web-interface.md) explains first-run setup, shared project state, local-model preparation, conversations, and the relationship to terminal and notebook interfaces.

## Platforms

- [Platform Support](platform-support.md) records implementation and validation status without implying institutional approval.
- [Stanford Carina](carina-cli.md) provides the native installation and synthetic GPU workflow.
- [Terra](terra-jupyter-demo.md) provides the custom-image, Jupyter, proxy, and synthetic validation workflow.

## Deployment

- [Container Images](container-images.md) documents generic and Terra-derived image tags, project mounts, security controls, and publication.
- [Platform Images](platform-images.md) defines how maintainers extend Heartwood from another platform base image.
- [Releases](releases.md) defines the protected release process.

## Technical Foundations

The numbered design set progresses from product scope to implementation details:

- [Overview](../design/01-overview.md) defines the mission, users, and reference workflow.
- [Platforms](../design/02-platforms.md) defines deployment assumptions and target-environment rationale.
- [System Architecture](../design/03-architecture.md) defines component ownership, the project and state contract, and runtime data flow.
- [Skills and Auto-Detection](../design/04-skills.md) defines Skill packaging, trust, detection, and extension behavior.
- [Security and Compliance](../design/05-security-compliance.md) defines threat boundaries and evidence requirements.
- [Observability, Audit, and Feedback](../design/06-observability-audit.md) defines in-boundary records, replay, and scrubbed export.
- [Testing and Evaluation](../design/07-testing-eval.md) defines test layers and model capability evidence.
- [Development Practices](../design/08-development.md) defines repository, toolchain, continuous-integration, and supply-chain policy.

The [Acronyms](../ACRONYMS.md) reference expands specialized terms used across these documents.

## Documentation Status

| Term | Meaning |
|---|---|
| Implemented | Code and repository tests exist for the behavior. |
| CI-validated | Automated checks exercise the behavior with synthetic data or controlled test infrastructure. |
| Live-validated | A published artifact has passed the documented synthetic workflow in the real platform control plane. |
| Institution-approved | The deploying institution approved the exact image, model route, credentials, data use, network controls, and evidence. Heartwood cannot assign this status. |
| Release-ready | Every acceptance gate for the declared release scope is reproducible from an immutable artifact. |

## Project Planning

[GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) own planned implementation, dependencies, and acceptance criteria. The [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) owns delivery status. Documentation describes current behavior and durable rationale; it does not serve as a backlog, implementation diary, or decision transcript.

## Published Documentation

The public [Heartwood documentation site](https://schmiedmayerlab.github.io/heartwood/) is built from the exact latest release tag. Pull requests and `main` validate the evolving source without changing the released site.

Preview the current source locally:

```bash
uv sync --locked --only-group docs
uv run --no-sync python deploy/stage_documentation.py
uv run --no-sync zensical serve
```

Use `uv run --no-sync zensical build --clean --strict` for the same strict build used by continuous integration.
