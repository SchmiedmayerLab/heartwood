<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Contribute Research Skills

Heartwood Skills are complete [Agent Skills](https://agentskills.io/specification) packages loaded through the public OpenHands Skill interface.
The [`heartwood-skills`](https://github.com/SchmiedmayerLab/heartwood-skills) repository owns curated Skill content, synthetic qualification, deterministic packaging, and catalog candidates.
Heartwood owns installation, project policy, OpenHands activation, interfaces, and audit records.

Use the Skill repository for a reusable research workflow.
Use the main Heartwood repository for changes to the runtime, model providers, action policy, project state, interfaces, or deployment behavior.

## Propose a Bounded Workflow

Open a Skill proposal before implementing a substantial workflow.
Define:

- the recurring research task and intended user;
- expected inputs, outputs, and stop conditions;
- decisions that remain with the researcher;
- required tools, network access, dataset types, and platforms;
- scripts, references, assets, and external dependencies; and
- a synthetic validation plan with relevant boundary cases.

Do not include credentials, participant-level data, model weights, private platform evidence, or generated research results in an issue, fixture, test, or log.

## Build the Complete Package

Each Skill has one directory under `skills/verified/`:

```text
skills/verified/example-skill/
├── SKILL.md
├── scripts/
├── references/
└── assets/
```

Only `SKILL.md` is required by the Agent Skills format.
Add supporting directories only when the workflow needs them:

- `scripts/` contains executable, deterministic workflow helpers;
- `references/` contains material the agent may load while applying the Skill; and
- `assets/` contains schemas, templates, or other static resources used by the workflow.

Keep instructions concise and place detailed executable or reference material in the appropriate package resource.
Do not duplicate standard Agent Skills metadata in another authored file.

## Declare Heartwood Policy

Curated Skills add Heartwood policy fields under the standard `metadata` map in `SKILL.md`:

| Field | Meaning |
|---|---|
| `heartwood.id` | Stable, globally unique Skill identity |
| `heartwood.version` | Semantic version of the Skill content contract |
| `heartwood.dataset-types` | Comma-separated dataset types the Skill understands |
| `heartwood.platforms` | Comma-separated supported Heartwood platforms |
| `heartwood.phi-risk` | Declared protected-health-information interaction class |
| `heartwood.requires-network` | Whether the Skill requires network access |
| `heartwood.controlled-data` | Repository status; curated content remains `not-approved` |
| `heartwood.approval-summary` | Compact description shown before project installation |
| `heartwood.entrypoint` | Optional package-relative executable entrypoint |

Declare the narrowest accurate permissions.
Repository review cannot approve a Skill for controlled data, grant a tool, or broaden model and network policy.

Heartwood currently rejects automatic dynamic shell context and embedded Model Context Protocol servers in curated Skills because no corresponding deployment policy is defined.
Propose the policy and runtime contract in Heartwood before relying on either feature.

## Test the Workflow

Set up the Skill repository with Python 3.12 and `uv`:

```bash
git clone https://github.com/SchmiedmayerLab/heartwood-skills.git
cd heartwood-skills
uv sync --locked
```

Run the complete local validation:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run vulture
uv run heartwood-skill-catalog validate
uv run heartwood-skill-catalog build
uv run pytest
```

Run validation and focused tests while editing.
The catalog build deliberately rejects modified or untracked content because its output must identify one immutable Git revision, so commit the complete change locally before running that command.

Tests for a script should cover the expected result, malformed input, boundary enforcement, and any aggregate-output or suppression decision.
Catalog validation loads every complete tree through OpenHands in strict mode, rejects undeclared or unsafe content, and verifies deterministic archives.

## Submit the Change

Use the repository-specific pull-request template.
Link the approved proposal, explain researcher-visible behavior, and report synthetic verification.
Review covers the complete package tree, including scripts, references, assets, permissions, and dependency changes.

A merged Skill is repository-reviewed but not automatically published to a deployment or approved for controlled data.
See [Skill Trust and Distribution](../architecture/skills.md#catalog-publication) for the separate candidate, signing, publication, and revocation process.
