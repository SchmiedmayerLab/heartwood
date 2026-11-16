<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Research Skills

A Skill gives the agent reusable instructions, scripts, and reference files for a research task, in the Agent Skills format that OpenHands supports.
Every release includes a small set of reviewed Skills; a deployment can offer more from a signed catalog, and anything you install is active only in the current project.

## See What Is Available

```bash
heartwood skills list
```

The browser **Skills** view shows the same list.
Each entry has a source and a status:

| Source | Meaning |
|---|---|
| `bundled` | Included and reviewed with this Heartwood release |
| catalog source ID | Offered as a signed, immutable package by a source the deployment configured |
| `installed` | Installed in this project from a catalog or a local directory |

| Status | Meaning |
|---|---|
| `active` | Given to the agent |
| `available` | Offered by a catalog and not installed |
| `revoked` | Withdrawn by its signed source and no longer given to the agent |
| `unsupported` | Not compatible with this platform; the reason is shown |

The browser also labels each Skill as **Repository reviewed** or **Local and unreviewed**, and says whether the deployment approved it for controlled data.

## Add a Reviewed Skill

Refresh the configured sources, inspect the Skill, and install it:

```bash
heartwood skills refresh
heartwood skills inspect SKILL_NAME
heartwood skills install SKILL_NAME
```

Inspection shows the description, declared tools, network and data-access requirements, dataset types, download size, source, and exact content digest.
The install command shows the current revision again and asks before continuing; it refreshes the signed source once more and stops if the content changed after your review.
In the browser, select the review button next to an available Skill.

Non-interactive automation must supply the digest from inspection instead of approving whichever revision is current:

```bash
heartwood skills install SKILL_NAME \
  --approve \
  --expected-tree-sha256 sha256:DIGEST_FROM_INSPECTION
```

With more than one configured source, add `--source SOURCE_ID` to `refresh`, `inspect`, or `install`.

## Add a Skill From This Project

A maintainer can install a complete Agent Skill directory from inside the project.
Its content has not passed repository review:

```bash
heartwood skills inspect-local ./skills/my-skill
heartwood skills install-local ./skills/my-skill
```

Heartwood rejects paths outside the project or inside `.heartwood/`, copies the complete directory without following links, binds the approval to its exact digest, and labels it **Local and unreviewed** in every interface.
The same interactive confirmation and digest options apply.

## Remove a Skill

```bash
heartwood skills remove SKILL_NAME
```

Removal deactivates the Skill, records the change, and resets open sessions so they no longer use it.
If unexpected files sit beside an installed Skill, Heartwood stops listing Skills and starting sessions until they are removed, and the error names the directory to clean up.

## What Approval Means

Installing a Skill grants no file, network, credential, model, or platform permission.
Action review, the project boundary, process permissions, and deployment policy still apply to everything the agent does with it.

Repository review is not controlled-data approval.
Only a deployment can approve an exact Skill digest for controlled data, and the interfaces report that decision separately.

See [Skill Trust and Distribution](../architecture/skills.md) for the verification model and [Configuration and State](../reference/configuration.md#skill-source-registries) for configuring sources.
