<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Add a Platform

A platform integration adapts storage, identity, scheduler, browser routing, model connections, and policy while retaining the shared Heartwood project, gateway, OpenHands, session, and interface contracts.
Do not add a platform-specific agent loop or separate web/CLI state.

The adapter implements `PlatformCapabilities`; `SessionGateway` remains the application boundary for every supported interface.

## Define Capabilities

Implement `PlatformAdapter.capabilities()` with:

- a stable platform identifier and display name;
- supported interfaces;
- browser routing mode;
- Heartwood-managed inference runtimes;
- scheduler behavior;
- durable-storage guidance;
- credential backends;
- permitted model-source categories;
- managed model connections; and
- validation level.

The gateway exposes this typed manifest at `GET /project/capabilities` and every interface uses it to hide unsupported choices.

## Detect the Environment

Detection must use deterministic, content-safe evidence such as explicit deployment markers, scheduler identity, or Jupyter platform variables.
Do not inspect research data or infer institutional authorization.

The generic adapter remains the fallback when no managed platform evidence matches.

## Define Policy and Connections

Provide a deny-by-default `PolicyProfile` containing exact model catalog and completion endpoints, allowed capability tiers, action-confirmation modes, and credential reference names.
Expose managed connection metadata without secret values.

User-entered compatible endpoints must not silently widen managed-platform policy.

## Package the Shared Application

Prefer extending the platform's supported base image with the shared Heartwood payload.
Preserve its entrypoint, user, Jupyter paths, proxy behavior, and required libraries unless a documented platform contract requires a change.

Declare image targets in `images/platforms.toml`, parameterize the shared `images/Dockerfile` assembly in `docker-bake.hcl`, and add platform-specific validation scripts only where the inherited environment requires them.

Keep model weights and credentials out of image layers.
Publish the manifest media type and architectures accepted by the platform, not a generic index when the platform cannot consume it.

## Validate Conformance

Add adapter protocol tests, capability serialization tests, read-only startup tests, project persistence tests, model-policy tests, credential-redaction tests, and interface tests for every advertised surface.

Use a production-derived CI image where the real base cannot run in pull-request CI, then validate the exact published artifact in a synthetic live environment before claiming live support.
