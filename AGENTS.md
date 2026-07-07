# AGENTS instructions

Guidance for humans and AI agents working in this repository.

## Purpose

This file is the repository orientation and rule set. It should point to the canonical docs instead of restating them.

When project direction changes, update the relevant design document first, then update this file only if routing or durable working rules change.

## Canonical docs

| Need | Source |
|---|---|
| Product summary and documentation index | [README.md](README.md) |
| Project scope, users, and reference workflow | [design/01-overview.md](design/01-overview.md) |
| Target platforms and deployment assumptions | [design/02-platforms.md](design/02-platforms.md) |
| Core architecture, adapter SPI, CLI-first interaction model, and data flow | [design/03-architecture.md](design/03-architecture.md) |
| `SKILL.md` packaging, metadata, detection, and sharing model | [design/04-skills.md](design/04-skills.md) |
| Security model, PHI handling, skill trust, and compliance kit | [design/05-security-compliance.md](design/05-security-compliance.md) |
| Audit log, tamper-evidence, activity view, and improvement loop | [design/06-observability-audit.md](design/06-observability-audit.md) |
| Testing layers, replay, model capability gates, and CI evaluation flow | [design/07-testing-eval.md](design/07-testing-eval.md) |
| Python-first toolchain, CI, repository hygiene, and supply chain | [design/08-development.md](design/08-development.md) |
| Phase plan, repository strategy, package layout, and first implementation backlog | [design/09-implementation-plan.md](design/09-implementation-plan.md) |
| Acronyms and named tools | [ACRONYMS.md](ACRONYMS.md) |

## Current implementation stance

- Follow [design/09-implementation-plan.md](design/09-implementation-plan.md) for the first implementation backlog and phase boundaries.
- Treat the CLI as the primary product surface, development harness, and CI target; notebook UI is a presentation adapter over the same session command/event contract. See [design/03-architecture.md](design/03-architecture.md) and [design/09-implementation-plan.md](design/09-implementation-plan.md).
- Use Python for the Phase 0 core, adapters, CLI, schemas, policy layer, audit log, replay tests, synthetic fixtures, Docker entrypoint, notebook API, and minimal notebook widgets. See [design/08-development.md](design/08-development.md).
- Keep one core repository through Phase 1 unless the repository strategy in [design/09-implementation-plan.md](design/09-implementation-plan.md) changes.
- Use synthetic fixtures only for initial development and CI. Live PHI must not be recorded into fixtures, replay traces, tests, or logs.

## Working rules

- Read the relevant design doc before editing implementation code in that area.
- Prefer the existing architecture: typed contracts, adapters at platform boundaries, deterministic fake providers for tests, and one shared session command/event model for all interfaces.
- Keep changes scoped to the requested behavior. Avoid unrelated refactors, metadata churn, or parallel architecture tracks.
- Add or update tests when changing detector logic, policy decisions, adapter behavior, skill validation, audit records, attestation export, CLI output, or notebook view models.
- Keep security and compliance claims evidence-backed. If a claim cannot be tested, audited, or linked to a platform control, document it as a limitation.
- Do not add a new implementation language, UI stack, service, registry, or repository split without updating the design docs that own that decision.

## Documentation rules

- Documentation should be standalone project material, not conversational or version-relative narrative.
- Avoid meta-commentary about how the document was created.
- Keep Markdown prose and list items on single logical lines; do not hard-wrap sentences.
- Preserve tables, headings, fenced code blocks, and intentional blank-line structure.
- Keep implementation plans compact, directive, and actionable.

## Acronyms

This project spans several jargon-heavy domains, so all acronyms are tracked in [ACRONYMS.md](ACRONYMS.md).

Whenever a new acronym is introduced anywhere in the project, add it to [ACRONYMS.md](ACRONYMS.md) with its expansion and a one-line description, placed in the appropriate group.

Keep glossary entries concise, keep groups roughly alphabetical, avoid duplicates, and update an existing entry if its meaning is clarified.
