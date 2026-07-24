<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Support and Compatibility

Heartwood uses Semantic Versioning and is under active pre-1.0 development.
This policy describes project maintenance; it is not a service-level agreement or an institutional support commitment.

## Supported Releases

The latest stable release is the maintained release line.
The project may issue a patch for that line when a confirmed security, data-integrity, installation, or release defect affects a supported configuration.
Older stable releases, prereleases, development branches, and moving `edge` image tags do not receive a maintenance window.

Before `1.0.0`, minor releases may change commands, configuration, persisted state, interfaces, platform requirements, and deployment artifacts.
Release notes and versioned documentation describe the supported behavior for each published version.

## Compatibility

A configuration is supported only when it appears in the versioned documentation for the selected release.
GPU deployments must also match the release's [GPU compatibility matrix](../reference/gpu-compatibility.md), including the platform, driver, CUDA runtime, model revision, precision, context size, tensor parallelism, and tool parser.

Heartwood validates the published native assets and container candidates for the exact release commit.
Live-platform qualification is stated separately from continuous-integration validation and expires when a material platform, driver, runtime, model, or policy component changes.

## Changes and Deprecation

Before `1.0.0`, the project favors a coherent interface over preserving unreleased or superseded behavior.
User-visible breaking changes must update the implementation, tests, release notes, and versioned documentation together.
A security or data-integrity concern may require immediate removal without a deprecation period.

After `1.0.0`, any change to the support window or deprecation policy requires a documented release decision before the affected release is published.

## Ownership

Repository `CODEOWNERS` identifies the current maintainer responsible for code, bundled Skill, and release review.
Only protected `main` commits that pass the release gate can be published, and the configured `release` environment requires explicit maintainer approval.
Repository administrators retain recovery authority for interrupted workflows, compromised credentials, and protected tags.

Report security concerns through the repository [security policy](https://github.com/SchmiedmayerLab/heartwood/security/policy).
Use [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) for reproducible defects and compatibility requests that do not contain sensitive information.
