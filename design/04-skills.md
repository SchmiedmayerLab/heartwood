<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Skills and Extensions

Heartwood uses the OpenHands `SKILL.md` format for reusable procedures. A Skill supplies instructions and optional scripts or resources to the agent context; it does not execute independently.

## Bundled Skills

The repository includes a small verified bundle for synthetic reference workflows. Verification means the source, metadata, scripts, fixture behavior, and OpenHands loading contract are tested in the repository. It does not mean clinical, statistical, security, license, or institutional certification.

Bundled Skills are read-only application content and are available to every project.

## Metadata

Heartwood adds metadata beside `SKILL.md` for:

- stable Skill identity and version;
- supported data and platform context;
- requested tools and network posture;
- expected input and output scope;
- review tier and source provenance; and
- compatible Heartwood version.

The OpenHands loader remains authoritative for the Skill format. Heartwood metadata adds deployment trust and audit information without creating another prompt or tool protocol.

## Project Extensions

Users can inspect and install a mounted local source:

```bash
heartwood skills inspect /path/to/skill
heartwood skills install /path/to/skill --approve
```

Installation:

1. validates metadata and the native OpenHands loader;
2. rejects symbolic links, path traversal, and unsupported files;
3. displays identity, trust tier, declared tools, network posture, and requested permissions for review;
4. copies into a temporary project-local directory;
5. verifies the copied contents; and
6. atomically publishes the extension and records the approval.

Installed extensions live in `.heartwood/skills/` and apply only to that project.

## Trust Boundary

Heartwood loads the bundled directory and explicitly installed project extensions. It does not automatically load arbitrary workspace, user-home, marketplace, or remote Skills.

The project owner remains responsible for reviewing the source instructions and scripts before installation. The resumable session can retain Skill invocation details; the content-minimized audit projection retains the tool category without invocation arguments. A Skill cannot bypass model policy or OpenHands action confirmation.
