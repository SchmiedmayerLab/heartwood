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
| Research specialist roles and boundaries | [documentation/use/specialists.md](documentation/use/specialists.md) |
| Command reference | [documentation/reference/cli.md](documentation/reference/cli.md) |
| Readiness states, stable diagnostics, and recovery steps | [documentation/reference/troubleshooting.md](documentation/reference/troubleshooting.md) |
| Qualified GPU runtime, model, and platform combinations | [documentation/reference/gpu-compatibility.md](documentation/reference/gpu-compatibility.md) |
| Product boundaries and durable technical rationale | [documentation/architecture/index.md](documentation/architecture/index.md) |
| Project, gateway, adapter, interface, and data-flow architecture | [documentation/architecture/system.md](documentation/architecture/system.md) |
| Security and controlled-data responsibilities | [documentation/operate/security.md](documentation/operate/security.md) |
| Authoritative audit exports, signing, and retention | [documentation/operate/audit-checkpoints.md](documentation/operate/audit-checkpoints.md) |
| Audit integrity and session persistence | [documentation/architecture/sessions-audit.md](documentation/architecture/sessions-audit.md) |
| Skill trust, distribution, activation, and interface contract | [documentation/architecture/skills.md](documentation/architecture/skills.md) |
| Research Skill contribution, policy, and validation | [documentation/contribute/skills.md](documentation/contribute/skills.md) |
| Testing layers and evidence language | [documentation/architecture/testing.md](documentation/architecture/testing.md) |
| Python and web development workflow | [documentation/contribute/development.md](documentation/contribute/development.md) |
| Pull request description structure | [Organization pull request template](https://github.com/SchmiedmayerLab/.github/blob/main/.github/pull_request_template.md) |
| Planned implementation, acceptance criteria, and delivery status | [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2) |
| Acronyms and specialized terms | [documentation/reference/glossary.md](documentation/reference/glossary.md) |

## Engineering Invariants

- The process current directory is the project boundary, and project-private configuration, models, sessions, and audit state live under `.heartwood/`.
- The gateway owns project behavior, persisted settings, session mutation, and interface projections. The terminal, browser, and notebook bridge adapt the same typed contracts rather than maintaining separate business rules.
- OpenHands owns the agent loop, conversation behavior, task tracking, and coding tools. Extend its public contracts through the existing adapter instead of introducing a parallel agent or tool implementation.
- Platform-specific behavior belongs in capability, policy, detector, launcher, and packaging adapters. It must not fork the application workflow.
- Generic and platform-derived artifacts share manifests, installers, image stages, runtime locks, and qualification scripts. Parameterize real platform differences rather than copying assembly logic.
- Images and installers contain inference software but no model weights or credentials. Model acquisition and secret delivery remain explicit runtime operations.
- Use Python for the application and shared contracts, and TypeScript with Grove for the researcher browser interface. Do not add another implementation language, UI stack, service, registry, or repository boundary without a reviewed architecture decision.
- Describe Heartwood by its purpose, behavior, and boundaries rather than using organizational attribution as the product description. Preserve required copyright and license metadata, and maintain individual credits in `CONTRIBUTORS.md`.

## Working Rules

- Read the owning architecture, operations, or reference page and inspect the current implementation before editing that area.
- Prefer established package ownership, typed contracts, and trusted dependencies over project-local alternatives. Add an abstraction only when it gives duplicated behavior one clear owner.
- Keep changes scoped, preserve unrelated worktree changes, and remove superseded paths completely when the requested behavior replaces them.
- Until Heartwood reaches `1.0.0`, prefer one clean, coherent contract over backward compatibility with unreleased commands, flags, environment variables, state layouts, APIs, or internal abstractions. Remove superseded paths completely and update implementation, tests, documentation, and release notes together; add a compatibility layer only when a documented data-integrity, security, or deployment requirement makes it necessary.
- Update the owning contract first when behavior is shared, then update every terminal, browser, notebook, platform, and packaging adapter that exposes it.
- Scale tests with risk. Test observable behavior, state transitions, failure recovery, and cross-interface consistency; avoid assertions tied only to prose, branding, formatting, or implementation trivia unless that exact value is a machine-consumed or security-critical contract.
- Add a regression test for a defect that escaped earlier checks whenever it can be reproduced deterministically at the appropriate layer.
- Use concise, descriptive branch names and title-style commit subjects. Do not use all-capital titles or identify an assistant or code-generation tool in branch names, commits, source files, documentation, or release artifacts.
- Prefer additive commits and ordinary pushes while a branch is under review. Rewrite published branch history only when an explicit correction requires it.
- Write pull request titles and descriptions as compact, natural project communication using the [organization template](https://github.com/SchmiedmayerLab/.github/blob/main/.github/pull_request_template.md). Include only decision-relevant context, release notes, documentation changes, and concise verification results; omit raw command output, development narration, and redundant detail.
- Do not post issue or pull-request comments without explicit approval. Resolving an already-addressed automated review thread is allowed.
- Never merge a pull request, enable auto-merge, queue a merge, or bypass a merge requirement without the user's explicit approval to merge that specific pull request. Passing checks or a general request to prepare a pull request does not constitute merge approval.

## Testing and Evidence

- Use deterministic fake providers for contract and failure-path tests, the real OpenHands SDK with `TestLLM` for conformance, and capable models only for the bounded acceptance path that requires real inference.
- Cover shared behavior at its owner and add interface tests for presentation differences; do not duplicate the same business-rule test independently in each interface.
- Exercise persistence and audit changes across interruption, retry, restart, concurrency, corruption, and replay boundaries appropriate to the change.
- Verify artifact changes through the shared assembly path and each affected platform contract. A platform-specific claim requires the evidence level defined in [Testing and Evidence](documentation/architecture/testing.md).
- Keep security, compatibility, and controlled-data claims evidence-backed. If a claim cannot be tested, audited, or linked to a platform control, state the limitation.
- Use synthetic fixtures only in source control, public examples, screenshots, CI, and public issue reproduction. Never place live protected health information in fixtures, replay traces, tests, screenshots, pull requests, or public logs.

## Documentation Rules

- Write for progressive disclosure: begin with a first successful workflow, then explain choices and recovery, and reserve implementation detail for architecture, operations, and reference pages.
- Documentation should be standalone project material, not conversational, version-relative, or development-process narrative.
- Keep current user guidance, operational instructions, reference material, and durable rationale in `documentation/`; keep planned implementation, acceptance criteria, dependencies, and delivery status in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2).
- State current support conservatively; do not present implemented or CI-validated behavior as live-validated or institution-approved.
- Keep commands, release tags, interface availability, screenshots, and compatibility tables synchronized with the implementation that publishes them.
- Avoid meta-commentary about how the document was created, validation transcripts, and future implementation discussions.
- Use semantic line breaks: place each complete prose sentence and each list item on its own source line; do not hard-wrap a sentence.
- Preserve tables, headings, fenced code blocks, and intentional blank-line structure.
- Keep run-specific timings, transcripts, failures, and validation evidence in CI artifacts or pull requests. Add only durable operational conclusions to user or architecture documentation.

## Acronyms

This project spans several jargon-heavy domains, so specialized terms used in public documentation are tracked in the [Glossary](documentation/reference/glossary.md).

When a new acronym or specialized platform term is introduced in public documentation, add it to the [Glossary](documentation/reference/glossary.md) when a first-time reader would need the definition.

Keep glossary entries concise, keep groups roughly alphabetical, avoid duplicates, and update an existing entry if its meaning is clarified.
