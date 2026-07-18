<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Security and Data Boundaries

Heartwood is designed to operate inside an existing research environment. It contributes application controls; it does not turn an unapproved environment or provider into an approved one.

## Trust Boundaries

The deployment owns:

- identity and user authorization;
- operating-system and container isolation;
- storage permissions, encryption, backup, and retention;
- network policy and egress enforcement;
- model-provider agreements and service configuration;
- dataset permissions and export controls;
- monitoring, incident response, and support.

Heartwood owns:

- exact model catalog and completion-route authorization;
- non-secret model settings and credential bindings;
- OpenHands action-confirmation configuration;
- project-confined file-editor operations;
- Skill source validation and explicit extension installation; and
- content-minimized audit records.

## Credentials

Credential values are not stored in project configuration, session events, logs, audit exports, images, or command-line arguments.

Interactive tokens remain in the current Heartwood process. Platform deployments may use mounted secrets or managed identity. Before constructing the OpenHands agent, Heartwood removes configured provider-key values from terminal subprocess environments.

This filtering reduces accidental exposure but does not isolate secrets from every same-user process. Deployments requiring a hard tool/credential boundary must provide process or workspace isolation.

## Agent Actions

Heartwood's file editor enforces the project boundary and excludes `.heartwood/`. Terminal actions retain the process's operating-system permissions and may reach other paths or network routes available to that process.

Action review is therefore one control within the deployment boundary, not a sandbox. Review complete action groups and apply platform controls appropriate to the data.

## Data and Logs

Resumable sessions contain prompts, model responses, proposed actions, and tool observations. They remain in project storage and may be sensitive.

Audit export omits prompt and response text, action payloads, paths, row values, secrets, and detailed error content by default. A scrubbed export still requires review before disclosure.

Heartwood sends no silent product telemetry. Public tests, examples, screenshots, and fixtures use synthetic data only.

## Authorization Claims

A working Heartwood artifact, route, Skill, or local model does not establish:

- a business associate agreement;
- Health Insurance Portability and Accountability Act eligibility;
- dataset authorization;
- acceptable retention or training settings;
- biomedical or clinical validity; or
- institutional approval.

Those decisions apply to the exact deployment and use case. The Schmiedmayer Lab at Stanford University maintains the open-source repository; each deploying organization owns its operational and institutional decisions.
