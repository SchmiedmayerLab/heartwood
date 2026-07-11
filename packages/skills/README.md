<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Skills

Local `SKILL.md` verification, package-time bundle catalog validation, and deterministic skill test helpers for Heartwood.

The package validates checked-in Skill directories before they can be loaded. Bundled repository-verified Skills must declare no network requirement, carry `heartwood.*` metadata, expose an approval summary, and point to a root-confined script entry point.

The package also validates `skills/bundle.toml`, the repository-local catalog selected for packaging. Local entries resolve to checked-in Skill directories. External Git records can describe a semver tag, resolved commit, content SHA-256 hash, `heartwood.*` metadata, and provenance placeholder, but no build-time importer or cryptographic signature verifier is implemented yet.
