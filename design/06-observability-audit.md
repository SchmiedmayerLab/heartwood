<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Audit and Reproducibility

Heartwood keeps two related records with different privacy and recovery purposes.

## Resumable Session

The project session preserves the researcher-facing conversation, action details, tool observations, lifecycle state, and OpenHands persistence required to continue work. Replay reconstructs the same ordered session after restart.

Session records remain inside the project boundary and may contain sensitive content. They are not an export format.

## Audit Record

The audit store derives content-minimized records from gateway events. It records:

- platform and model-route decisions;
- action mode and risk classification;
- action-group membership;
- allow or reject decisions;
- tool names, status, and exit codes;
- Skill installation decisions and the generic tool category used for session activation;
- errors represented by bounded codes; and
- export creation.

It excludes raw prompts, model responses, command text, file contents, paths, row values, credentials, and detailed exceptions by default.

## Tamper Evidence

Audit entries form a local hash chain. This detects changes within the exported sequence; it does not provide an external timestamp, signature, immutable store, or independent custody.

The deployment may copy reviewed exports into its evidence system. Heartwood does not silently transmit them.

## Reproducibility

Session replay shows what the researcher and agent observed, including Skill invocation details retained by the session. Local-model provenance records the immutable model source and artifact identity. Installed Skill metadata records the available procedure version. These elements support investigation and repetition but do not prove that a model response or scientific result will be identical.

Only synthetic sessions may become public fixtures or repository replay tests.
