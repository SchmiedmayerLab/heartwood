<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Skills

Local `SKILL.md` verification, package-time bundle catalog validation, and deterministic skill test helpers for Heartwood.

The package validates checked-in skill directories before they can be loaded by the session harness. The Phase 0 implementation is intentionally offline: verified skills must declare no network requirement, carry `heartwood.*` metadata, expose an approval summary, and point to a root-confined script entry point.

The package also validates `skills/bundle.toml`, the repository-local catalog of skills selected for packaging. Local entries resolve to verified checked-in skill directories. External git entries are not fetched at runtime; they must carry a semver tag, a resolved commit, a content SHA-256 hash, `heartwood.*` metadata, and Sigstore provenance so a build-time importer can vendor them into the same local directory shape.
