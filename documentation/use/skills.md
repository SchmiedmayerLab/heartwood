<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Research Skills

Skills are versioned instruction packages that help OpenHands perform a repeatable workflow with declared tools, data expectations, and review guidance.
Heartwood includes repository-verified Skills and can install project-scoped extensions after explicit inspection and approval.

## Inspect Available Skills

```bash
heartwood skills list
```

The browser **Skills** view presents the same catalog.
Bundled Skills ship with the Heartwood release; installed extensions live under `.heartwood/skills/` for the current project.

## Inspect an Extension

Before installing a Skill from a mounted folder:

```bash
heartwood skills inspect /path/to/skill
```

Review the source, metadata, declared tools, expected data access, and provenance outside Heartwood as well.
Skill validation checks structure and integrity; it does not establish that third-party instructions are trustworthy for controlled data.

## Install or Remove an Extension

```bash
heartwood skills install /path/to/skill --approve
heartwood skills remove skill-name
```

Installation copies the reviewed Skill into private project state and records the operation.
Removing it resets active agent services so later turns cannot continue using the removed instructions.

## Data Boundaries

Skills do not grant new filesystem, network, credential, or platform permissions.
The project boundary, OpenHands tools, process permissions, model-route policy, and deployment controls remain authoritative.

Use synthetic fixtures for public examples, tests, and shared Skill development.
