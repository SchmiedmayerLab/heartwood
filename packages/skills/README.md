<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Skills

Verification, acquisition, and project-scoped storage for complete Agent Skill packages.

The shared `heartwood-skill-catalog` package owns strict OpenHands loading, complete-tree manifests, deterministic archives, and confined extraction and copying.
Heartwood adds deployment-owned TUF source configuration, signature and expiry verification, exact-digest approval, atomic content-addressed installation, signed revocation handling, and revalidation before a Skill reaches OpenHands.

Curated source content is maintained in the pinned [`heartwood-skills`](https://github.com/SchmiedmayerLab/heartwood-skills) repository.
The CLI, browser, and notebook bridge consume the same gateway projection and do not maintain independent Skill policy.

Repository review and controlled-data approval are separate.
A deployment may approve only an exact verified Skill tree digest; catalog metadata and local packages cannot grant that status.
