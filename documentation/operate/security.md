<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Security and Controlled Data

Heartwood is designed to run inside a research environment with explicit project, model, action, Skill, and audit controls.
It is not a security boundary on its own and does not confer institutional approval, HIPAA compliance, or authorization to process protected health information.

## Threat Boundaries

### Project Files

OpenHands tools execute with the permissions of the Heartwood process.
Heartwood supplies the project directory as the agent workspace and rejects project-state paths in normal tool scope, but the operating system, container, or platform must enforce stronger isolation when required.

Use a dedicated project directory and least-privilege mounts.
Do not run Heartwood as a privileged container or from a broad shared root.

### Model Content

Prompts, selected project content, tool results, and summaries may be sent to the active model route.
The platform policy can deny unlisted endpoints, but infrastructure egress controls and provider agreements remain authoritative.

Confirm data eligibility for the exact endpoint, account, model, and deployment before use.

### Credentials

Raw credentials are excluded from project configuration, browser storage, command arguments, durable session events, logs, and audit exports by design.
Heartwood resolves process values, operator bindings, optional system-keyring entries, or platform identity only for named provider calls.

Project-scoped keyring persistence is explicit and available only where a functional system credential store exists.
Custom compatible-service tokens remain process-only.

### Skills and Instructions

A Skill can influence agent behavior and tool selection.
Structural validation and provenance records do not make third-party instructions trustworthy.

Review Skill source and declared tools, install only through an approved path, and treat instructions embedded in project files or external content as potentially untrusted.

### Audit Data

The audit log is hash-chained and export is scrubbed, but operational metadata can still be sensitive.
Store, retain, share, and delete audit artifacts under the same reviewed records policy as the surrounding project.

## Recommended Controls

- authenticate every user before they reach the execution environment;
- isolate projects and users with platform permissions or containers;
- deny network egress except reviewed model and package endpoints;
- mount controlled inputs read-only when feasible;
- keep provider secrets in a keyring, mounted secret, or managed identity;
- use **Ask Every Time** until a deployment-specific risk policy is reviewed;
- pin release artifacts, images, model revisions, and Skill versions;
- collect content-minimized operational logs outside project outputs;
- validate backup, retention, deletion, and incident-response procedures; and
- perform synthetic end-to-end validation before controlled-data enablement.

## Claims and Evidence

Describe a deployment as suitable for a data class only when the institution can point to the relevant platform controls, agreements, security review, and validation evidence.
An implemented or CI-tested Heartwood route is not equivalent to live deployment approval.
