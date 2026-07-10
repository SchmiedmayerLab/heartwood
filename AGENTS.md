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
| Product summary and documentation index | [README.md](README.md) |
| Documentation roles and status vocabulary | [docs/README.md](docs/README.md) |
| Current platform implementation and validation status | [docs/platform-support.md](docs/platform-support.md) |
| Project scope, users, and reference workflow | [design/01-overview.md](design/01-overview.md) |
| Target platforms and deployment assumptions | [design/02-platforms.md](design/02-platforms.md) |
| Core architecture, session gateway, adapter SPI, interaction surfaces, and data flow | [design/03-architecture.md](design/03-architecture.md) |
| `SKILL.md` packaging, metadata, detection, and sharing model | [design/04-skills.md](design/04-skills.md) |
| Security model, PHI handling, skill trust, and compliance kit | [design/05-security-compliance.md](design/05-security-compliance.md) |
| Audit log, tamper-evidence, activity view, and improvement loop | [design/06-observability-audit.md](design/06-observability-audit.md) |
| Testing layers, replay, model capability gates, and CI evaluation flow | [design/07-testing-eval.md](design/07-testing-eval.md) |
| Python-first toolchain, CI, repository hygiene, and supply chain | [design/08-development.md](design/08-development.md) |
| Current baseline, readiness gaps, delivery priorities, and acceptance gates | [design/09-implementation-plan.md](design/09-implementation-plan.md) |
| Acronyms and specialized terms | [ACRONYMS.md](ACRONYMS.md) |

## Current Implementation Stance

- Follow [design/09-implementation-plan.md](design/09-implementation-plan.md) for delivery priorities and acceptance gates.
- Treat the CLI as the primary development and CI surface; the notebook bridge and researcher web UI are presentation adapters over the same session command/event contract, served through the session gateway and its OpenHands SDK backend. See [design/03-architecture.md](design/03-architecture.md) and [design/09-implementation-plan.md](design/09-implementation-plan.md).
- Use Python for the core, session gateway, OpenHands adapter, model settings, adapters, CLI, schemas, policy layer, audit log, replay tests, synthetic fixtures, Docker entrypoint, notebook API, and widgets; use TypeScript on the Stanford Spezi web stack for the researcher web UI. See [design/08-development.md](design/08-development.md).
- Keep one core repository through the controlled-data reference workflow unless the repository strategy in [design/09-implementation-plan.md](design/09-implementation-plan.md) changes.
- Use synthetic fixtures only in source control, public examples, and CI. Live PHI must not be recorded into fixtures, replay traces, tests, or public logs.

## Working Rules

- Read the relevant design doc before editing implementation code in that area.
- Prefer the existing architecture: typed contracts, adapters at platform boundaries, deterministic fake providers for tests, one shared session command/event model for all interfaces, OpenHands-owned conversations and coding tools, and a gateway-owned OpenHands adapter as the only agent path.
- Keep changes scoped to the requested behavior. Avoid unrelated refactors, metadata churn, or parallel architecture tracks.
- Add or update tests when changing detector logic, policy decisions, adapter behavior, skill validation, audit records, attestation export, CLI output, notebook view models, or web-UI view models.
- Keep security and compliance claims evidence-backed. If a claim cannot be tested, audited, or linked to a platform control, document it as a limitation.
- Do not add a new implementation language, UI stack, service, registry, or repository split without updating the design docs that own that decision.

## Documentation Rules

- Documentation should be standalone project material, not conversational or version-relative narrative.
- Keep current operational instructions in `docs/`, durable rationale in `design/01` through `design/08`, and delivery priorities and acceptance gates in `design/09-implementation-plan.md`.
- Use the status terms defined in [docs/README.md](docs/README.md); do not present implemented or CI-validated behavior as live-validated or institution-approved.
- Avoid meta-commentary about how the document was created.
- Keep Markdown prose and list items on single logical lines; do not hard-wrap sentences.
- Preserve tables, headings, fenced code blocks, and intentional blank-line structure.
- Keep the delivery roadmap compact, ordered, directive, and acceptance-driven.

## Acronyms

This project spans several jargon-heavy domains, so all acronyms are tracked in [ACRONYMS.md](ACRONYMS.md).

Whenever a new acronym is introduced anywhere in the project, add it to [ACRONYMS.md](ACRONYMS.md) with its expansion and a one-line description, placed in the appropriate group.

Keep glossary entries concise, keep groups roughly alphabetical, avoid duplicates, and update an existing entry if its meaning is clarified.
