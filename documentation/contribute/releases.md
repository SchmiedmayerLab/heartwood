<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Release Guide

A release is created by the **Create Release** workflow from the exact current `main` commit after that commit's required checks pass.
Versions use strict Semantic Versioning without a `v` prefix, such as `0.5.0` or `0.5.0-beta.1`.

## Prepare a Release

1. Update `VERSION.toml` and every version-owned package, lock, image, installer, and documentation reference together; `deploy/verify_release_candidate.py` lists any that differ.
2. Run the complete test and documentation suites.
3. Merge the reviewed change to `main` and wait for **Main Validation** to pass on that commit.
4. Start **Create Release** from `main` with the exact version.
5. Review the draft release: generated notes, assets, checksums, qualification evidence, and target commit.
6. Approve the `release` environment to publish.

## Release Workflow Checks

The workflow stops unless all of the following hold for the commit:

- the version is valid, matches every version-owned source, and has no published release or tag;
- the commit is still the tip of `main`, checked again before the draft and before publication;
- every catalog model source verifies, and the curated Skill revision is published on `heartwood-skills` `main`;
- the `Release Candidate Ready`, `Analyze (python)`, and `Analyze (javascript-typescript)` checks passed;
- the Native Qualification workflow passed for the commit and its evidence verifies against the commit and its attestation;
- immutable container candidates exist for the commit.

`Release Candidate Ready` requires every Main Validation job, including capable-model acceptance.
On `main`, the separate **Native Qualification** workflow qualifies and attests every commit without cancelling earlier runs; see [Native Qualification Evidence](../architecture/testing.md#native-qualification-evidence).
Release creation downloads that run's evidence and attestation, verifies them with `gh attestation verify` bound to this repository, the Native Qualification workflow, `refs/heads/main`, and the commit, and with `deploy/qualification_evidence.py`, and fails when either is missing or does not verify.
It then writes `agent-safety-drills.json` from that evidence for the routes in `deploy/release-model-routes.toml`.
Pull-request runs are not attested.

The workflow then builds the native assets from the commit, tests the installer layout and an installation from an empty Ubuntu container, attests every asset, and creates a draft release with GitHub-generated notes.
After approval it publishes the immutable image tags and the release, then the versioned documentation.

## Authority

`CODEOWNERS` identifies the current release maintainer.
The protected `release` environment is the release-authority boundary: it accepts deployments only from protected branches and requires the maintainer's approval.
The **Semantic Version Releases** tag ruleset forbids updating, deleting, or force-moving any tag; organization administrators can bypass it.
Administrators may recover an interrupted workflow, which replaces an unpublished draft for the same commit, but must never replace or retarget a published release.

## Release Contents

| Item | Contents |
|---|---|
| `heartwood-installer` | Native installer script |
| `heartwood-native.tar.gz` | Application source, curated Skills, maintained runtime wheels, and browser assets |
| vLLM wheel | The CUDA 12.9 wheel pinned in `images/gpu/compatibility.toml`, for restricted environments such as Carina |
| `SHA256SUMS` | Checksums of the native archive and the vLLM wheel |
| Qualification evidence | `native-qualification-evidence.tar.gz`, the commit's native qualification evidence with its `SHA256SUMS`, and `native-qualification-evidence.sigstore.jsonl`, its attestation bundle, verified as described in [Native Qualification Evidence](../architecture/testing.md#native-qualification-evidence) |
| `agent-safety-drills.json` | The [agent safety drill](../architecture/testing.md#agent-safety-drills) results of that evidence for every supported model route |
| Image tags | `VERSION`, `VERSION-gpu-nvidia`, `VERSION-terra`, and `VERSION-terra-gpu-nvidia`, promoted from the commit's candidate images |
| Documentation | The `VERSION` path of the versioned site |

Every release asset carries a build provenance attestation.
Releases contain no worker image, sandbox gateway, model weights, or credentials.
The platform contract bundle is not a release asset; partners export it from the release commit as described in [Platform Contract](../operate/platform-contract.md#generate-and-review-the-current-artifacts).
A prerelease is marked as a GitHub prerelease and never becomes `latest`.

## Stable and Preview Documentation

A stable version such as `0.5.0` moves the `stable` alias and the documentation root to it.
A prerelease such as `0.5.0-beta.1` moves the `preview` alias without changing the root.
Documentation is published only for the latest release of its channel, and republishing a version with different content is rejected.

See [Support and Compatibility](../operate/support.md) for the maintained release line and pre-1.0 change policy.
