<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# AGENTS Instructions

Guidance for contributors working in this repository.

## Purpose

This file is the repository orientation and rule set. It should point to the canonical docs instead of restating them.

When project direction changes, update the relevant architecture or operations page first, then update this file only if routing or durable working rules change.

## Canonical Documentation

| Need | Source |
|---|---|
| Repository summary | [README.md](README.md) |
| Published documentation home and user journey | [documentation/index.md](documentation/index.md) |
| First-use installation, project, model, and interface flow | [documentation/start/index.md](documentation/start/index.md) |
| Workstation, Terra, Carina, and managed-environment selection | [documentation/platforms/index.md](documentation/platforms/index.md) |
| Installation routes | [documentation/start/install.md](documentation/start/install.md) |
| Project boundary, persistence, and `.heartwood/` layout | [documentation/start/project.md](documentation/start/project.md) |
| Research-environment, hosted, compatible-service, and Heartwood-managed model workflows | [documentation/models/index.md](documentation/models/index.md) |
| Deployment responsibilities and platform extension contract | [documentation/operate/index.md](documentation/operate/index.md) |
| Release support, compatibility, and deprecation policy | [documentation/operate/support.md](documentation/operate/support.md) |
| Browser workflow | [documentation/use/browser.md](documentation/use/browser.md) |
| Command reference | [documentation/reference/cli.md](documentation/reference/cli.md) |
| Readiness states, stable diagnostics, and recovery steps | [documentation/reference/troubleshooting.md](documentation/reference/troubleshooting.md) |
| Qualified GPU runtime, model, and platform combinations | [documentation/reference/gpu-compatibility.md](documentation/reference/gpu-compatibility.md) |
| Product boundaries and durable technical rationale | [documentation/architecture/index.md](documentation/architecture/index.md) |
| Project, gateway, adapter, interface, and data-flow architecture | [documentation/architecture/system.md](documentation/architecture/system.md) |
| Security and controlled-data responsibilities | [documentation/operate/security.md](documentation/operate/security.md) |
| Audit integrity and session persistence | [documentation/architecture/sessions-audit.md](documentation/architecture/sessions-audit.md) |
| Testing layers and evidence language | [documentation/architecture/testing.md](documentation/architecture/testing.md) |
| Python and web development workflow | [documentation/contribute/development.md](documentation/contribute/development.md) |
| Pull request description structure | [Schmiedmayer Lab pull request template](https://github.com/SchmiedmayerLab/.github/blob/main/.github/pull_request_template.md) |
| Planned implementation, acceptance criteria, and delivery status | [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) |
| Acronyms and specialized terms | [documentation/reference/glossary.md](documentation/reference/glossary.md) |

## Current Implementation Stance

- Follow the relevant [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) for planned implementation, acceptance criteria, and delivery status.
- Treat the CLI as the primary development and CI surface; the notebook bridge and researcher web UI are presentation adapters over the same session command/event contract, served through the session gateway and its OpenHands SDK backend. See [documentation/architecture/system.md](documentation/architecture/system.md).
- Use Python for the core, session gateway, OpenHands adapter, model settings, adapters, CLI, schemas, policy layer, audit log, replay tests, synthetic fixtures, Docker entrypoint, notebook API, and widgets; use TypeScript on the Stanford Spezi web stack for the researcher web UI. See [documentation/contribute/development.md](documentation/contribute/development.md).
- Keep one core repository through the controlled-data reference workflow unless a reviewed design change establishes independent ownership, release cadence, and versioned contracts.
- Use synthetic fixtures only in source control, public examples, and CI. Live PHI must not be recorded into fixtures, replay traces, tests, or public logs.
- Attribute Heartwood only to the Schmiedmayer Lab at Stanford University; do not add legacy organizational attributions.

## Working Rules

- Read the relevant architecture, operations, or reference page before editing implementation code in that area.
- Prefer the existing architecture: typed contracts, adapters at platform boundaries, deterministic fake providers for tests, one shared session command/event model for all interfaces, OpenHands-owned conversations and coding tools, and a gateway-owned OpenHands adapter as the only agent path.
- Keep business rules, labels, setup choices, readiness, and persisted state in the gateway or their owning typed package; CLI, browser, and notebook code should only adapt those shared projections to their interaction style.
- Assemble generic and platform-derived artifacts through shared manifests, installers, and image stages. Parameterize genuine platform differences instead of copying dependency installation, application assembly, or validation logic.
- Until Heartwood reaches `1.0.0`, prefer one clean, coherent contract over backward compatibility with unreleased commands, flags, environment variables, state layouts, APIs, or internal abstractions. Remove superseded paths completely and update implementation, tests, documentation, and release notes together; add a compatibility layer only when a documented data-integrity, security, or deployment requirement makes it necessary.
- Keep changes scoped to the requested behavior. Avoid unrelated refactors, metadata churn, or parallel architecture tracks.
- Write pull request titles and descriptions as compact, natural project communication using the [organization template](https://github.com/SchmiedmayerLab/.github/blob/main/.github/pull_request_template.md). Include only decision-relevant context, release notes, documentation changes, and concise verification results; omit raw command output, development narration, and redundant detail.
- Never merge a pull request, enable auto-merge, queue a merge, or bypass a merge requirement without the user's explicit approval to merge that specific pull request. Passing checks or a general request to prepare a pull request does not constitute merge approval.
- Add or update tests when changing detector logic, policy decisions, adapter behavior, skill validation, audit records, attestation export, CLI output, notebook view models, or web-UI view models.
- Keep security and compliance claims evidence-backed. If a claim cannot be tested, audited, or linked to a platform control, document it as a limitation.
- Do not add a new implementation language, UI stack, service, registry, or repository split without updating the architecture or operations page that owns that decision.

## Documentation Rules

- Documentation should be standalone project material, not conversational or version-relative narrative.
- Keep current user guidance, operational instructions, reference material, and durable rationale in `documentation/`; keep planned implementation, acceptance criteria, dependencies, and delivery status in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2).
- State current support conservatively; do not present implemented or CI-validated behavior as live-validated or institution-approved.
- Avoid meta-commentary about how the document was created.
- Use semantic line breaks: place each complete prose sentence and each list item on its own source line; do not hard-wrap a sentence.
- Preserve tables, headings, fenced code blocks, and intentional blank-line structure.
- Do not use project documentation as a development log, backlog, or implementation discussion.
- Keep run-specific timings, transcripts, failures, and validation evidence in CI artifacts or pull requests. Add only durable operational conclusions to user or architecture documentation.

## Acronyms

This project spans several jargon-heavy domains, so specialized terms used in public documentation are tracked in the [Glossary](documentation/reference/glossary.md).

When a new acronym or specialized platform term is introduced in public documentation, add it to the [Glossary](documentation/reference/glossary.md) when a first-time reader would need the definition.

Keep glossary entries concise, keep groups roughly alphabetical, avoid duplicates, and update an existing entry if its meaning is clarified.
