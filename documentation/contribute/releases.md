<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Release Guide

Heartwood releases are created by an approval-gated GitHub Actions workflow after required checks pass for the exact `main` commit.
Release versions use strict Semantic Versioning without a `v` prefix.

## Prepare a Release

1. Update `VERSION.toml` and every version-owned package, Skill, lock, image, installer, and documentation reference together.
2. Run the complete test and documentation suites.
3. Merge the reviewed change to `main` only after required checks pass.
4. Start the **Create Release** workflow with the exact version.
5. Review the editable draft, generated release notes, native assets, checksums, and target commit.
6. Approve the `release` environment to publish.

The workflow verifies immutable container candidates, builds and tests native assets, creates a draft with GitHub-generated notes, waits for approval, publishes immutable image tags and the GitHub Release, then publishes versioned documentation.

`CODEOWNERS` identifies the current release maintainer.
The protected `release` environment is the release-authority boundary and requires explicit approval after the candidate has passed the exact-commit gate.
Repository administrators may recover interrupted workflows, but they must not replace or retarget an existing immutable release.

## Stable and Preview Documentation

A stable version such as `0.2.0` updates the `stable` alias and the documentation root.
A prerelease such as `0.3.0-beta.1` updates the `preview` alias without replacing the stable root.

The version store is deployed to GitHub Pages and retains immutable version paths.
Publishing the same version with different content is rejected.

## Release Artifacts

Each release includes:

- `heartwood-installer`;
- `heartwood-native.tar.gz`;
- `SHA256SUMS`;
- standard, GPU, Terra, and Terra GPU image tags; and
- versioned documentation.

The release workflow marks prerelease versions as GitHub prereleases and never moves a `latest` release designation to them.

See [Support and Compatibility](../operate/support.md) for the maintained release line and pre-1.0 change policy.
