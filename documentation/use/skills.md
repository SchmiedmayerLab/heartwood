<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Research Skills

Skills give Heartwood reusable instructions, scripts, references, and supporting files for a research workflow.
They use the Agent Skills format supported by OpenHands.

Heartwood starts with a small set of reviewed Skills included in every release.
Your research environment may also offer additional reviewed Skills from a signed catalog.
Anything you install is active only for the current project.

## See What Is Available

```bash
heartwood skills list
```

The browser **Skills** view shows the same list and status.

Entries are labeled as:

- **Bundled:** included and reviewed with this Heartwood release.
- **Available:** offered by a signed source configured by the deployment.
- **Installed:** approved and stored for this project.
- **Revoked:** withdrawn by its signed source and no longer available to the agent.
- **Unsupported:** not compatible with the current platform.

## Add a Reviewed Skill

Refresh the configured sources, then inspect a Skill before installing it:

```bash
heartwood skills refresh
heartwood skills inspect SKILL_NAME
heartwood skills install SKILL_NAME --approve
```

Inspection shows the description, declared tools, network requirement, download size, source, and exact content digest.
Installation refreshes the signed source again and stops if the content changed after inspection.

If more than one source is configured, add `--source SOURCE_ID` to `refresh`, `inspect`, or `install`.

## Add a Skill From This Environment

A maintainer may provide a complete Agent Skill directory outside a catalog.
This is an advanced path because the content has not passed repository review:

```bash
heartwood skills inspect-local /path/to/skill
heartwood skills install-local /path/to/skill --approve
```

Heartwood verifies and copies the complete directory without following links, binds approval to its exact digest, and labels it **Local and unreviewed** in every interface.

Remove an installed Skill with:

```bash
heartwood skills remove SKILL_NAME
```

## What Approval Means

Skill installation does not grant filesystem, network, credential, model, or platform permissions.
OpenHands tools, the project boundary, action confirmation, process permissions, and deployment policy remain authoritative.

Repository review is also not controlled-data approval.
Only a deployment can approve an exact Skill digest for controlled data, and the interface reports that decision separately.

See [Skill Trust and Distribution](../architecture/skills.md) for the verification model and [Configuration and State](../reference/configuration.md#skill-source-registries) for deployment source configuration.
