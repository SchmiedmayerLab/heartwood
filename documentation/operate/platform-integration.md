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
- supported gateway ingress modes and one default;
- Heartwood-managed inference runtimes;
- scheduler behavior;
- durable-storage guidance;
- credential backends;
- the model-credential boundary;
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

## Gateway Ingress

Choose from the shared ingress modes instead of adding platform-specific request parsing:

- `direct-loopback` for a client that connects directly to a loopback gateway;
- `jupyter-proxy` for an authenticated local Jupyter proxy that strips one exact external prefix; or
- `trusted-proxy` for a non-loopback deployment with exact proxy source ranges, external origin, base path, and forwarded metadata.

The deployment supplies route values to `heartwood gateway serve`.
The gateway validates and normalizes them once for the REST API, server-sent events, WebSockets, browser assets, and generated browser base path.
Interface clients do not inspect Terra, Carina, Jupyter, or proxy-specific environment variables.

A trusted proxy must remove inbound forwarding and identity headers before setting its own complete values.
Restrict its upstream network route to the configured gateway bind.
Heartwood validates that request metadata agrees with the declared route; the proxy remains responsible for end-user authentication, authorization, TLS, and network isolation.

## Model Credential Isolation

Leave `platform_isolated_model_sources` empty unless an exact model source uses a transport separated from the operating-system identity that runs OpenHands tools.
Application scrubbing keeps secrets out of supported tool inputs and persisted surfaces, but it does not justify unattended secret-backed operation.

Add a model source to `platform_isolated_model_sources` only when the adapter has `ci-and-live-synthetic` validation and the deployment demonstrates that terminal, file-editor, Task, and future supported tools cannot read that source's credentials.
Qualification is source-specific: isolating an institution-managed gateway must not qualify a subscription, hosted-provider, or custom endpoint on the same platform.
The selected profile must use the exact model-source identifier and a managed-identity binding; an environment variable or mounted credential file remains application-scrubbed even when another route on the platform is isolated.
The validation must cover restart, every advertised interface, project and `.heartwood/` files, inherited process resources, logs, events, browser and notebook outputs, and audit exports.
Model authorization and tool execution must remain independently revocable.

OpenHands RemoteWorkspace is useful for isolating an agent environment from its caller.
Because its Agent Server still owns both model orchestration and tools, it is not a model-only credential boundary without an additional platform control.
Prefer a platform-native model proxy or separately authorized service identity over a Heartwood-managed secret store.

## Package the Shared Application

Prefer extending the platform's supported base image with the shared Heartwood payload.
Preserve its entrypoint, user, Jupyter paths, proxy behavior, and required libraries unless a documented platform contract requires a change.

Declare image targets in `images/platforms.toml`, parameterize the shared `images/Dockerfile` assembly in `docker-bake.hcl`, and add platform-specific validation scripts only where the inherited environment requires them.

Keep model weights and credentials out of image layers.
Publish the manifest media type and architectures accepted by the platform, not a generic index when the platform cannot consume it.

## Validate Conformance

Add adapter protocol tests, capability serialization tests, ingress adversarial tests, read-only startup tests, project persistence tests, model-policy tests, credential-boundary tests, and interface tests for every advertised surface.

Use a production-derived CI image where the real base cannot run in pull-request CI, then validate the exact published artifact in a synthetic live environment before claiming live support.
