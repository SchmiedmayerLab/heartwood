<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Documentation

Start with [Get Started with Heartwood](getting-started.md). It explains how to choose an installation path, open a project, connect a model, and begin a conversation without requiring knowledge of the internal architecture.

## Start Here

- [Get Started with Heartwood](getting-started.md) explains when to use a container, native installation, or platform image and follows the first project through model setup and conversation.
- [Work with the Agent](using-heartwood.md) explains conversations, action review, session controls, resume, and audit export.
- [Use the Browser and Notebooks](web-interface.md) explains visual setup, shared project state, local-model preparation, conversations, and Jupyter routing.

## Models

- [Connect a Model](model-connections.md) explains the choices shown during model setup and how credentials are handled.
- [Run a Model Locally](getting-started-offline.md) explains model weights, inference servers, recommended and user-selected Hugging Face downloads, automatic runtime selection, managed launch, and no-network validation.

## Platforms

- [Choose Where to Run Heartwood](platforms.md) compares workstation, container, Terra, Carina, and other managed environments.
- [Heartwood on Terra](terra-jupyter-demo.md) provides the custom-image, Jupyter, proxy, and synthetic validation workflow.
- [Heartwood on Stanford Carina](carina-cli.md) provides the native installation and synthetic GPU workflow.

## Deployment

- [Deploy Heartwood](deployment.md) introduces artifact selection, persistence, model access, security boundaries, and validation claims.
- [Container Images](container-images.md) documents generic and Terra-derived image tags, project mounts, security controls, and publication.
- [Build a Platform-Specific Image](platform-images.md) defines how maintainers extend Heartwood from another platform base image.
- [Platform Support and Validation](platform-support.md) records release-specific evidence without implying institutional approval.
- [Release Heartwood](releases.md) defines the protected release process.

## How Heartwood Works

These documents progress from product intent to implementation and validation details:

- [Product Scope](../design/01-overview.md) defines the mission, users, boundaries, and reference workflow.
- [System Architecture](../design/03-architecture.md) defines component ownership, the project and state contract, and runtime data flow.
- [Security and Compliance](../design/05-security-compliance.md) defines threat boundaries and evidence requirements.
- [Skills and Extensions](../design/04-skills.md) defines Skill packaging, trust, detection, and extension behavior.
- [Audit and Reproducibility](../design/06-observability-audit.md) defines in-boundary records, replay, and scrubbed export.
- [Platform Architecture](../design/02-platforms.md) defines deployment assumptions and target-environment rationale.
- [Testing and Evaluation](../design/07-testing-eval.md) defines test layers and model capability evidence.
- [Engineering and Releases](../design/08-development.md) defines repository, toolchain, continuous-integration, and supply-chain policy.

## Reference

- [Project Files and State](project-state.md) defines the current-directory project boundary and `.heartwood/` layout.
- [Glossary and Acronyms](../ACRONYMS.md) explains specialized terms used throughout the project.
- [Contributing](../CONTRIBUTING.md) defines the contributor workflow and local checks.

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
