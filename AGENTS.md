<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# AGENTS Instructions

Guidance for contributors working in this repository.

## Purpose

This file is the repository orientation and rule set. It should point to the canonical docs instead of restating them.

When project direction changes, update the relevant design document first, then update this file only if routing or durable working rules change.

## Canonical Documentation

| Need | Source |
|---|---|
| Repository summary | [README.md](README.md) |
| Published documentation home and user journey | [documentation/index.md](documentation/index.md) |
| First-use installation, project, model, and interface flow | [docs/getting-started.md](docs/getting-started.md) |
| Workstation, Terra, Carina, and managed-environment selection | [docs/platforms.md](docs/platforms.md) |
| Native installation | [docs/installation.md](docs/installation.md) |
| Project boundary, persistence, and `.heartwood/` layout | [docs/project-state.md](docs/project-state.md) |
| Local model selection, download, runtime, and offline workflow | [docs/getting-started-offline.md](docs/getting-started-offline.md) |
| Deployment artifact, persistence, model-route, and validation responsibilities | [docs/deployment.md](docs/deployment.md) |
| Current platform implementation and validation status | [docs/platform-support.md](docs/platform-support.md) |
| Browser workflow, model setup, actions, audit, CLI parity, and notebook layout | [docs/web-interface.md](docs/web-interface.md) |
| Command reference | [docs/cli-reference.md](docs/cli-reference.md) |
| Readiness states, common setup and runtime failures, and safe diagnostic collection | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Project scope, users, and reference workflow | [design/01-overview.md](design/01-overview.md) |
| Target platforms and deployment assumptions | [design/02-platforms.md](design/02-platforms.md) |
| Project and state contract, core architecture, session gateway, adapter SPI, interaction surfaces, and data flow | [design/03-architecture.md](design/03-architecture.md) |
| `SKILL.md` packaging, metadata, detection, and sharing model | [design/04-skills.md](design/04-skills.md) |
| Security model, PHI handling, skill trust, and compliance kit | [design/05-security-compliance.md](design/05-security-compliance.md) |
| Audit log, tamper-evidence, activity view, and improvement loop | [design/06-observability-audit.md](design/06-observability-audit.md) |
| Testing layers, replay, model capability gates, and CI evaluation flow | [design/07-testing-eval.md](design/07-testing-eval.md) |
| Python-first toolchain, CI, repository hygiene, and supply chain | [design/08-development.md](design/08-development.md) |
| Planned implementation, acceptance criteria, and delivery status | [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) |
| Acronyms and specialized terms | [ACRONYMS.md](ACRONYMS.md) |

## Current Implementation Stance

- Follow the relevant [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) for planned implementation, acceptance criteria, and delivery status.
- Treat the CLI as the primary development and CI surface; the notebook bridge and researcher web UI are presentation adapters over the same session command/event contract, served through the session gateway and its OpenHands SDK backend. See [design/03-architecture.md](design/03-architecture.md).
- Use Python for the core, session gateway, OpenHands adapter, model settings, adapters, CLI, schemas, policy layer, audit log, replay tests, synthetic fixtures, Docker entrypoint, notebook API, and widgets; use TypeScript on the Stanford Spezi web stack for the researcher web UI. See [design/08-development.md](design/08-development.md).
- Keep one core repository through the controlled-data reference workflow unless a reviewed design change establishes independent ownership, release cadence, and versioned contracts.
- Use synthetic fixtures only in source control, public examples, and CI. Live PHI must not be recorded into fixtures, replay traces, tests, or public logs.
- Attribute Heartwood only to the Schmiedmayer Lab at Stanford University; do not add legacy organizational attributions.

## Working Rules

- Read the relevant design doc before editing implementation code in that area.
- Prefer the existing architecture: typed contracts, adapters at platform boundaries, deterministic fake providers for tests, one shared session command/event model for all interfaces, OpenHands-owned conversations and coding tools, and a gateway-owned OpenHands adapter as the only agent path.
- Until Heartwood reaches `1.0.0`, prefer one clean, coherent contract over backward compatibility with unreleased commands, flags, environment variables, state layouts, APIs, or internal abstractions. Remove superseded paths completely and update implementation, tests, documentation, and release notes together; add a compatibility layer only when a documented data-integrity, security, or deployment requirement makes it necessary.
- Keep changes scoped to the requested behavior. Avoid unrelated refactors, metadata churn, or parallel architecture tracks.
- Never merge a pull request, enable auto-merge, queue a merge, or bypass a merge requirement without the user's explicit approval to merge that specific pull request. Passing checks or a general request to prepare a pull request does not constitute merge approval.
- Add or update tests when changing detector logic, policy decisions, adapter behavior, skill validation, audit records, attestation export, CLI output, notebook view models, or web-UI view models.
- Keep security and compliance claims evidence-backed. If a claim cannot be tested, audited, or linked to a platform control, document it as a limitation.
- Do not add a new implementation language, UI stack, service, registry, or repository split without updating the design docs that own that decision.

## Documentation Rules

- Documentation should be standalone project material, not conversational or version-relative narrative.
- Keep current operational instructions in `docs/`, durable rationale in the numbered `design/` documents, and planned implementation, acceptance criteria, dependencies, and delivery status in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2).
- State current support conservatively; do not present implemented or CI-validated behavior as live-validated or institution-approved.
- Avoid meta-commentary about how the document was created.
- Keep Markdown prose and list items on single logical lines; do not hard-wrap sentences.
- Preserve tables, headings, fenced code blocks, and intentional blank-line structure.
- Do not use project documentation as a development log, backlog, or implementation discussion.
- Keep run-specific timings, transcripts, failures, and validation evidence in CI artifacts or pull requests. Add only durable operational conclusions to user or design documentation.

## Acronyms

This project spans several jargon-heavy domains, so specialized terms used in public documentation are tracked in [ACRONYMS.md](ACRONYMS.md).

When a new acronym or specialized platform term is introduced in public documentation, add it to [ACRONYMS.md](ACRONYMS.md) when a first-time reader would need the definition.

Keep glossary entries concise, keep groups roughly alphabetical, avoid duplicates, and update an existing entry if its meaning is clarified.
