<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Support and Compatibility

Heartwood uses [Semantic Versioning 2.0.0](https://semver.org/) and has not reached `1.0.0`.
This policy describes project maintenance; it is not a service-level agreement or an institutional support commitment.

## Supported Releases

The latest stable release is the maintained release line.
A patch for that line is issued when a confirmed security, data-integrity, installation or release defect affects a supported configuration.
Older stable releases, prereleases, development branches and the moving `edge` image tags receive no maintenance.

A release is published only from a protected `main` commit whose `Release Candidate Ready` check passed, which includes the native worker qualification and the attestation of its evidence.
Published release contents are immutable, and the release notes and versioned documentation describe the supported behavior of each version.

## Versioning Rules

- Before `1.0.0`, a minor release may change commands, configuration, persisted state, interfaces, platform requirements and deployment artifacts.
- A patch release preserves compatibility, including validation rules and documented behavior.
- From `1.0.0`, incompatible public-contract changes increment the major version, compatible additions the minor version, and compatible fixes the patch version.
- The [platform contract bundle](platform-contract.md#versioning) has its own SemVer version, independent of the application; upstream protocols keep their own versions.

## Compatibility

A configuration is supported only when the versioned documentation of the selected release describes it.
GPU deployments must also match that release's [GPU compatibility matrix](../reference/gpu-compatibility.md), including platform, driver, CUDA runtime, model revision, precision, context size, tensor parallelism and tool parser.

Support levels follow [Testing and Evidence](../architecture/testing.md#claims), and each claim names its level:

| Configuration | Support level |
|---|---|
| Workstation, container and native installation with local execution | CI validated |
| Terra terminal and notebook, Stanford Carina terminal | CI validated |
| Isolated worker on a dedicated Linux Docker engine with OpenShell | Native qualification against the [sandbox gateway build](platform-requirements.md#sandbox-gateway), with synthetic and benign workloads |
| Sensitive or regulated data | Not supported in any configuration |

Releases do not publish a worker image or a sandbox gateway image; the sandbox gateway is built from the release's commit as described in [Sandbox Gateway](platform-requirements.md#sandbox-gateway).
Native qualification evidence binds one commit, one set of image contents and one host, and does not transfer to a rebuilt image, another architecture or another sandbox gateway; see [Sandbox Gateway](platform-requirements.md#sandbox-gateway).
Live-platform qualification is stated separately from CI validation and expires when a material platform, driver, runtime, model, sandbox or policy component changes.

## Update Cadence

Review the latest stable release at least once per quarter and apply a published security patch promptly.
Before each update, read the release notes, back up the research files with their matching state, and record the new artifact digest in the [standard environment record](index.md#record-a-standard-environment).
After the update, review the deployment manifest against the release's [manifest reference](../reference/configuration.md#deployment-manifest) and repeat [Validate the Deployment](index.md#validate-the-deployment).

Project state migrates forward only: an older release does not open state that a newer release has migrated.

## Changes and Deprecation

Before `1.0.0`, the project favors one coherent interface over preserving superseded behavior, and removes superseded commands, settings and schemas without a deprecation period.
A user-visible breaking change updates the implementation, tests, release notes and versioned documentation together.
A security or data-integrity concern can require immediate removal at any version.
After `1.0.0`, any change to the support window or deprecation policy requires a documented release decision before the affected release is published.

## Ownership and Reporting

Repository `CODEOWNERS` identifies the current maintainer responsible for code, bundled Skill and release review.
The configured `release` environment requires explicit maintainer approval to publish, and repository administrators retain recovery authority for interrupted workflows, compromised credentials and protected tags.

Report security concerns through the repository [security policy](https://github.com/SchmiedmayerLab/heartwood/security/policy).
Use [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) for reproducible defects and compatibility requests that contain no sensitive information.
