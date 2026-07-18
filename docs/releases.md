<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Release Heartwood

This page is for maintainers. Heartwood uses Semantic Versioning without a `v` prefix. `VERSION.toml` is the canonical version, and repository checks require package metadata, lockfiles, Skills, and versioned guide examples to agree.

## Prepare the Release

1. Update the canonical version and every checked package reference in a reviewed pull request.
2. Confirm that the exact `main` commit has completed **Release Candidate Ready** and the repository-required external checks.
3. Start the protected workflow:

```bash
gh workflow run create-release.yml --ref main -f version=0.2.0-beta.3
```

The workflow verifies the version, commit, native installer, generic and Terra images, NVIDIA variants, and release assets. It creates a draft with GitHub-generated notes before waiting for approval in the protected `release` environment.

The approving maintainer may refine the draft title and notes but must not replace the verified assets. Publication rechecks the commit and artifacts, creates the immutable tag and GitHub Release, and promotes the verified image descriptors.

Prerelease versions are marked as GitHub prereleases, never become `Latest`, and update only the documentation `preview` channel. Stable releases update `stable` and the site default.

## Recover Documentation Publication

If only the Pages job fails for the latest release in its channel, rerun:

```bash
gh workflow run publish-documentation.yml --ref main -f version=0.2.0-beta.3
```

The recovery workflow verifies the tag, release, commit, channel, and existing version content before deployment. It cannot move a channel backward.
