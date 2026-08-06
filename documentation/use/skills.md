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

| Source | What the user sees |
|---|---|
| Heartwood release | **Bundled** Skills maintained with Heartwood |
| Deployment catalog | **Available** Skills published as signed immutable packages by Heartwood or another configured operator |
| Current project | **Local and unreviewed** packages added explicitly by a maintainer |

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
heartwood skills install SKILL_NAME
```

Inspection shows the description, declared tools, network and data-access requirements, dataset types, download size, source, and exact content digest.
The interactive install command shows the current revision again and asks before continuing.
Installation refreshes the signed source again and stops if the content changes after that review.

Non-interactive automation must supply the exact digest from inspection instead of approving whichever revision is current:

```bash
heartwood skills install SKILL_NAME \
  --approve \
  --expected-tree-sha256 sha256:DIGEST_FROM_INSPECTION
```

If more than one source is configured, add `--source SOURCE_ID` to `refresh`, `inspect`, or `install`.

## Add a Skill From This Environment

A maintainer may place a complete Agent Skill directory inside the current project.
This is an advanced path because the content has not passed repository review:

```bash
heartwood skills inspect-local ./skills/my-skill
heartwood skills install-local ./skills/my-skill
```

Heartwood rejects paths outside the project or inside `.heartwood`, verifies and copies the complete directory without following links, binds approval to its exact digest, and labels it **Local and unreviewed** in every interface.
Local installation uses the same interactive confirmation and digest-bound automation options as signed catalog installation.

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
