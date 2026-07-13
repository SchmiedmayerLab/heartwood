<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 06 — Observability, Audit, And Feedback

## Two Records

OpenHands persists conversation state for execution and resume. Heartwood translates the relevant conversation events into its versioned session command/event stream and derives a separate hash-chained audit log. The OpenHands store is the execution substrate; the Heartwood log is the product and compliance record consumed by the CLI, notebook bridge, web UI, replay, and export.

Per session, Heartwood records:

- commands and actor identity;
- platform and dataset detection proposals with evidence and confidence;
- selected model profile identifier, which equals the connection identifier for catalog-selected models, endpoint, capability tier, action-confirmation mode, and route decision without credentials or the provider catalog;
- researcher and agent message events in the in-boundary session stream;
- proposed tool name and risk in both records, with the model-generated action summary retained only in the in-boundary session stream;
- confirmation request and allow-once or reject result when the selected mode requires review;
- tool completion status in both records, with detailed result presentation retained only in the in-boundary session stream;
- Skill identity, trust, verification, and installation decision when applicable;
- export request, policy decision, and attestation metadata, with filesystem destinations scrubbed from the exported audit record;
- errors without prompt, response, secret, or row content.

The exported audit record omits message content, model-generated action summaries, filesystem paths, row values, and sensitive tool payloads. These fields remain available only where required in the in-boundary operational state.

## Tamper Evidence

Each Heartwood audit event includes the previous event hash. Editing, deleting, or reordering persisted records breaks chain verification. This detects local modification but does not prevent a user with filesystem control from deleting the entire record. Deployment-owned signing, checkpointing, and authoritative copies are separate controls required where institutional policy demands them.

## Researcher Activity

The CLI, notebook, and web UI render the same plain-language sequence from the shared event stream. The conversation remains primary; route policy, selected confirmation mode, Skill identity, action risk, confirmations, tool status, and export details are available without forcing a researcher to approve deployment policy or repository-verified Skill activation repeatedly. Auto-approved low-risk actions still produce proposed-action and execution events, so reduced prompting does not remove activity or audit visibility.

## Replay And Resume

Heartwood event replay reconstructs the complete researcher-facing transcript, activity state, and pending action set. OpenHands conversation persistence restores execution state. The gateway adapter owns the mapping between these stores so clients never depend on OpenHands private persistence formats.

## Field Feedback Boundary

Heartwood does not export field-feedback trajectories. Any such export must be researcher initiated, separately authorized, deterministically scrubbed, and confirmed by a human to contain no protected health information; [Issue #49](https://github.com/SchmiedmayerLab/heartwood/issues/49) owns the privacy and purpose-limitation gates. Raw prompts, model responses, tool payloads, row values, and credentials do not leave the deployment by default.

Validated synthetic fixtures can become replay tests and Skill improvements. Improvements return as reviewed code or new images rather than hidden telemetry; external Skill releases remain subject to the distribution boundary in [04](04-skills.md).

## Anti-Goals

Heartwood does not send silent telemetry, export raw protected data, treat local hash chaining as immutable storage, or require a network service to persist and inspect the audit trail.
