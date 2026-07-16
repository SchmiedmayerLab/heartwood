<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Release Heartwood

Heartwood releases use Semantic Versioning without a `v` prefix. Examples include `0.2.0`, `1.0.0`, and `1.2.0-rc.1`. The Git tag, GitHub Release, native installer version, and primary container tag use the same version. Because Open Container Initiative tag syntax does not permit `+`, build metadata uses `_` only in container tags; for example, Git release `1.2.0+build.3` maps to image tag `1.2.0_build.3`.

## Create a Release

`VERSION.toml` is the canonical release version. Prepare a release through a reviewed pull request that updates this value, every workspace and web package version, runtime version constants, lockfiles, and versioned user-guide examples together. CI rejects inconsistent package or guide versions, and the release workflow refuses an input that differs from the canonical source version. Python source declarations retain the Semantic Versioning spelling, while Python lock metadata uses the equivalent normalized Python version, such as `0.2.0b2` for `0.2.0-beta.2`.

Start the protected workflow from the current `main` branch:

```bash
gh workflow run create-release.yml --ref main -f version=0.2.0-beta.2
```

Every `main` commit runs the `Main Validation` workflow. Its dependency graph calls the repository validation, CodeQL, Python, web, secret scan, container smoke, native asset, CPU image, and GPU image workflows and emits `Release Candidate Ready` only after every dependency succeeds. The release workflow accepts only strict Semantic Versioning, requires every packaged source version to match, refuses an existing tag or published release, and binds the candidate to the current `main` commit. It checks `Release Candidate Ready` once for that exact commit and fails immediately when main validation is absent, incomplete, skipped, cancelled, or failed. It then verifies the immutable generic, Terra, generic GPU, and Terra GPU images and rebuilds and tests the versioned native installation bundle.

After automated verification, the workflow creates a draft with GitHub's automatically generated release notes and the verified native assets. The `Approve And Publish Release` job then waits in the protected `release` environment. Before approving, the designated maintainer can open the draft and refine its title or notes; release assets must remain unchanged. The maintainer may approve their own deployment. Approval authorizes only the already-verified commit, draft, and artifacts; it does not bypass a failed gate. Publication rechecks the draft assets and confirms that `main` still points to the approved commit, then stops if either changed while approval was pending.

For every release, the same workflow checks out the exact published tag, rebuilds the canonical documentation strictly, and deploys it to a version-specific path on [GitHub Pages](https://schmiedmayerlab.github.io/heartwood/) through the `github-pages` environment. Stable releases move the `/stable/` alias and make it the site default. Versions with a Semantic Versioning prerelease suffix are automatically published as GitHub prereleases, are never designated `Latest`, and move only the `/preview/` alias. Before the first stable documentation release, the site root opens the preview; subsequent prereleases cannot replace the stable default. Pull requests and `main` validate documentation changes but never deploy them.

The Zensical-supported Mike integration maintains `gh-pages` as a generated version store. GitHub Pages remains configured for workflow deployment and serves the verified Actions artifact, so the generated branch does not introduce a second publication path.

The publication job creates these immutable version tags from the verified commit images:

- `ghcr.io/schmiedmayerlab/heartwood:<version>`
- `ghcr.io/schmiedmayerlab/heartwood:<version>-terra`
- `ghcr.io/schmiedmayerlab/heartwood:<version>-gpu-nvidia`
- `ghcr.io/schmiedmayerlab/heartwood:<version>-terra-gpu-nvidia`

The workflow attests `heartwood-installer`, `heartwood-native.tar.gz`, and `SHA256SUMS` before creating the editable draft. After approval, it publishes the versioned images and the Git tag and immutable GitHub Release from the exact commit. Repository rules protect tags against update, force-move, and deletion, with organization-administrator bypass retained only for recovery before publication; immutable-release enforcement locks the published tag and assets. The workflow refuses a pre-existing tag, so the approved GitHub Actions release job remains the supported publication path. Restricting creation itself would require a separately managed GitHub App because the repository `GITHUB_TOKEN` cannot bypass an Actions-only creation rule; Heartwood does not introduce a long-lived maintainer token for this purpose.

## Maintain the Gate

Release-required workflows are declared as jobs in `.github/workflows/main-validation.yml` and as dependencies of its `release-ready` job. When a release requirement is added, removed, or renamed, update both parts of that dependency graph in the same pull request. `Main Validation` is the sole orchestrator on pull requests and `main`, so each component executes once and the same dependency graph governs merge and release readiness. Pull requests skip only main-only image publication while retaining container smoke and GPU candidate validation. Component workflows remain independently dispatchable for diagnostics. The main ruleset requires `Release Candidate Ready` plus the default CodeQL language analyses, CodeRabbit, and dependency review, which execute outside the graph; it does not duplicate internal component job names.

The release workflow is intentionally serialized. If publication is interrupted after a versioned image tag is created, a rerun accepts that tag only when it resolves to the same verified digest. If an interruption leaves a draft release for the exact candidate commit, the approved publication job replaces that draft with freshly verified assets before publishing. A published release, a draft for another commit, or a version tag that points elsewhere is never overwritten.

If only the Pages deployment fails after the latest release in its channel is public, rerun it from `main` without changing the release:

```bash
gh workflow run publish-documentation.yml --ref main -f version=0.2.0-beta.2
```

The recovery workflow verifies the canonical version, Git tag, release state, target commit, and stable or preview channel position before it can update the site. It rejects older releases so recovery cannot move either public channel backward. Existing version content must match on a rerun, and the generated branch is pushed only after every local publication check succeeds.
