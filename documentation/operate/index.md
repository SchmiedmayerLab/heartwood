<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Deploy Heartwood

This section is for platform operators and security reviewers.
A Heartwood deployment combines a versioned application artifact with platform storage, identity, network, secret, compute, model-route, logging, and data-governance controls.

## Deployment Responsibilities

| Layer | Heartwood Provides | Platform or Institution Provides |
|---|---|---|
| Application | CLI, browser, notebook bridge, gateway, OpenHands adapter, Skills, policy, audit | Artifact approval and release selection |
| Project | Current-directory boundary and private `.heartwood/` state | Durable storage, permissions, backup, retention, and deletion |
| Identity | Credential-binding interfaces and content-safe status | User authentication, authorization, managed identity, and secret delivery |
| Models | Provider catalogs, Heartwood-managed inference planning, route-policy evaluation | Approved endpoints, agreements, accounts, quotas, and data eligibility |
| Compute | llama.cpp/vLLM launch contracts and Slurm/provisioned adapters | CPU/GPU capacity, drivers, scheduler, isolation, and cost controls |
| Network | Deny-by-default model-route policy | Enforced egress, ingress authentication, proxies, DNS, and segmentation |
| Evidence | Session events, tamper-evident audit chain, scrubbed export, CI artifacts | Central monitoring, incident response, records policy, and compliance evidence |

Heartwood policy is defense in depth and does not replace network enforcement.
The browser service must remain on loopback or behind an authenticated platform proxy.

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

## Validate the Deployment

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
10. enforced network behavior, including a no-network Heartwood-managed inference test when offline operation is claimed.

Record live validation evidence outside public user documentation and never include protected data in a fixture or transcript.
