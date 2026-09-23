<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Deploy Heartwood

This section is for platform operators and security reviewers.

Read [Support and Compatibility](support.md) before selecting a release for a maintained deployment.
For a group of researchers, [record a standard environment](#record-a-standard-environment) and [run a pilot evaluation](#run-a-pilot-evaluation) before a wider rollout.
A Heartwood deployment combines a versioned application artifact with platform storage, identity, network, secret, compute, model-route, logging, and data-governance controls.

## Deployment Responsibilities

| Layer | Heartwood Provides | Platform or Institution Provides |
|---|---|---|
| Application | CLI, browser, notebook bridge, gateway, OpenHands adapter, Skills, policy, audit | Artifact approval and release selection |
| Project | Current-directory boundary and private `.heartwood/` state | Durable storage, permissions, backup, retention, and deletion |
| Identity | Launch capability on every gateway API request, event stream, and WebSocket, credential-binding interfaces, content-safe status, and credential-boundary enforcement | User authentication, authorization, launch capability delivery, managed identity, secret delivery, and isolated model identities |
| Models | Provider catalogs, Heartwood-managed inference planning, route-policy evaluation | Approved endpoints, agreements, accounts, quotas, and data eligibility |
| Compute | llama.cpp/vLLM launch contracts and Slurm/provisioned adapters | CPU/GPU capacity, drivers, scheduler, isolation, and cost controls |
| Network | Deny-by-default model-route policy and strict declared ingress validation | Enforced egress, ingress authentication and authorization, proxy header sanitation, TLS, DNS, and segmentation |
| Evidence | Session events, tamper-evident audit chain, scrubbed export, provider-neutral signed checkpoint format, CI artifacts | Signer service and key custody, authoritative storage, retention enforcement, central monitoring, incident response, and compliance evidence |

Heartwood policy is defense in depth and does not replace network enforcement.
The [Platform Contract](platform-contract.md) defines this boundary and each capability a platform supplies in detail.
The browser service must remain on loopback or behind an authenticated platform proxy configured through the trusted ingress contract.

## Select an Artifact

- Use the multi-platform standard image for generic AMD64/ARM64 deployments.
- Use the NVIDIA image for AMD64 vLLM deployments and verify the selected configuration against the [GPU compatibility matrix](../reference/gpu-compatibility.md).
- Use a Terra-specific single-platform image for Terra Leonardo.
- Use the release native installer where containers are not the platform's normal execution mechanism.

Pin a release tag or digest.
Do not deploy moving `edge` tags for reproducible or controlled work.

## Persist the Right Data

Mount or assign one dedicated project directory as the process current directory.
Persist the project and `.heartwood/`; keep installation files, temporary runtime files, and job scratch separate.

Do not pre-create `.heartwood/` with an incompatible layout.
Opening the browser and running `heartwood doctor` are read-only; explicit project confirmation or the first mutating operation initializes state.

## Provide Model Connections

A platform adapter can advertise managed connections and credential backends.
Operator-supplied model manifests define non-secret connection metadata, while platform policy defines allowed catalog and completion endpoints, credential references, capability tiers, and action-confirmation modes.

Never add raw tokens to container layers, image labels, project configuration, command arguments, examples, or CI logs.
The built-in Terra, Carina, and generic policies permit both action-review modes; a deployment that must forbid **Low-Risk Automation** needs a platform policy that omits it.
Secret-backed routes require **Review Every Action** unless the active platform reports a live-qualified platform-isolated model credential boundary for that exact source.
See [Security and Controlled Data](security.md#model-credential-isolation) for the distinction between application scrubbing and platform isolation.

## Validate the Deployment

Concurrent advisory reviews are optional and require retained quality and performance evidence for the configured route.
See [Qualify Parallel Reviews](parallel-reviews.md); deployments without evidence retain sequential review.

Before real data, use a synthetic project to verify:

1. exact artifact digest and platform capability response;
2. project persistence across process or compute restart;
3. model discovery and a real agent response;
4. an OpenHands-compatible structured tool proposal;
5. grouped allow and reject behavior;
6. tool execution confined to the project;
7. terminal, browser, and notebook parity where advertised;
8. replay and scrubbed audit export;
9. no secret values in configuration, events, logs, or exports; and
10. ingress host, origin, forwarding, prefix, and source rejection behavior;
11. the declared model-credential boundary and action-policy restriction; and
12. enforced network behavior, including a no-network Heartwood-managed inference test when offline operation is claimed; and
13. signed checkpoint creation, external retention, independent verification, and synthetic restore.

Record live validation evidence outside public user documentation and never include protected data in a fixture or transcript.

## Record a Standard Environment

When several researchers share one setup, keep one record of it with the deployment's operating documentation, outside every research project.
Verify each researcher's environment against the record with the checks above before project data is added.

| Record | Source |
|---|---|
| Release tag and verified digest | [Select an Artifact](#select-an-artifact) |
| Platform image, compute shape, and storage | The platform guide, such as [Terra](../platforms/terra.md) |
| Approved model routes | [Choose Where Models Run](../models/index.md#data-and-compliance-boundary) |
| Action-review mode | [Actions and Audit History](../use/actions-audit.md) |
| Signer profile and checkpoint retention | [Audit Checkpoints and Retention](audit-checkpoints.md) |
| Analysis dependency lock | [Verify an Existing Analysis](../use/research-workflows.md#verify-an-existing-analysis) |
| Owner and next update review | [Support and Compatibility](support.md#update-cadence) |

## Run a Pilot Evaluation

A pilot shows whether a small group of researchers can install, use, and trust one standard environment before a wider rollout.
Use synthetic or approved non-sensitive data, keep the release pinned, and name a support contact.

1. Agree on exit criteria before the first participant starts.
2. Onboard each participant until they have made one action decision.
3. Have every participant run the three [research workflows](../use/research-workflows.md) in order on the same synthetic inputs.
4. Collect the evidence below, then list the changes a wider rollout or a second platform requires.

| Question | Evidence |
|---|---|
| Could participants start? | Installation success and time to the first action decision |
| Did the workflows complete and reproduce? | `heartwood experiments list --json` and the verification report |
| Is the record intact? | `heartwood audit verify` |
| What did support require? | Requests and time to resolution |
| Would participants use it? | Structured feedback |

Collect counts and outcomes, not prompts, project files, or session history.
Report the results as live validation in that environment; see [Testing and Evidence](../architecture/testing.md#claims).
