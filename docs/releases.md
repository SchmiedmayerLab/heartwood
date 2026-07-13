<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Releases

Heartwood releases use Semantic Versioning without a `v` prefix. Examples include `0.1.0`, `1.0.0`, and `1.2.0-rc.1`. The Git tag, GitHub Release, native installer version, and primary container tag use the same version. Because Open Container Initiative tag syntax does not permit `+`, build metadata uses `_` only in container tags; for example, Git release `1.2.0+build.3` maps to image tag `1.2.0_build.3`.

## Create A Release

`VERSION.toml` is the canonical release version. Prepare a release through a reviewed pull request that updates this value, every workspace and web package version, runtime version constants, lockfiles, and versioned user-guide examples together. CI rejects inconsistent package or guide versions, and the release workflow refuses an input that differs from the canonical source version.

Start the protected workflow from the current `main` branch:

```bash
gh workflow run create-release.yml --ref main -f version=0.1.0
```

The workflow accepts only strict Semantic Versioning, requires every packaged source version to match, and refuses an existing tag or published release. It binds the candidate to the current `main` commit, waits for every check in `.github/release-required-checks.txt`, verifies the immutable generic, Terra, generic GPU, and Terra GPU images for that commit, and builds and tests the native installation bundle. A failed, cancelled, skipped, missing, stale, or incomplete required check prevents publication.

After automated verification, the workflow creates a draft with GitHub's automatically generated release notes and the verified native assets. The `Approve And Publish Release` job then waits in the protected `release` environment. Before approving, the designated maintainer can open the draft and refine its title or notes; release assets must remain unchanged. The maintainer may approve their own deployment. Approval authorizes only the already-verified commit, draft, and artifacts; it does not bypass a failed gate. Publication rechecks the draft assets and confirms that `main` still points to the approved commit, then stops if either changed while approval was pending.

The publication job creates these immutable version tags from the verified commit images:

- `ghcr.io/schmiedmayerlab/heartwood:<version>`
- `ghcr.io/schmiedmayerlab/heartwood:<version>-terra`
- `ghcr.io/schmiedmayerlab/heartwood:<version>-gpu-nvidia`
- `ghcr.io/schmiedmayerlab/heartwood:<version>-terra-gpu-nvidia`

The workflow attests `heartwood-installer`, `heartwood-native.tar.gz`, and `SHA256SUMS` before creating the editable draft. After approval, it publishes the versioned images and the Git tag and immutable GitHub Release from the exact commit. Repository rules protect tags against update, force-move, and deletion, with organization-administrator bypass retained only for recovery before publication; immutable-release enforcement locks the published tag and assets. The workflow refuses a pre-existing tag, so the approved GitHub Actions release job remains the supported publication path. Restricting creation itself would require a separately managed GitHub App because the repository `GITHUB_TOKEN` cannot bypass an Actions-only creation rule; Heartwood does not introduce a long-lived maintainer token for this purpose.

## Maintain The Gate

When a release-required workflow job is added or renamed, update `.github/release-required-checks.txt` in the same pull request. Add only checks that execute on every `main` commit. Pull-request-only checks remain enforced by the `main` ruleset and cannot be required again on the squash commit.

The release workflow is intentionally serialized. If publication is interrupted after a versioned image tag is created, a rerun accepts that tag only when it resolves to the same verified digest. If an interruption leaves a draft release for the exact candidate commit, the approved publication job replaces that draft with freshly verified assets before publishing. A published release, a draft for another commit, or a version tag that points elsewhere is never overwritten.
