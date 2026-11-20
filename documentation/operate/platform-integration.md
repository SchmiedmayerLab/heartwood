<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Add a Platform

A platform adapter adapts storage, identity, scheduling, browser routing, model connections and policy to one environment while the project, gateway, OpenHands, session and interface contracts stay shared.
Do not add a platform-specific agent loop or separate terminal or browser state.
The adapter implements the `PlatformAdapter` protocol in `heartwood.adapters`; start from the [Platform Contract](platform-contract.md) for what the platform itself must provide.

## Define Capabilities

`PlatformAdapter.capabilities()` returns a `PlatformCapabilities` record with:

- `platform_id` and `display_name`;
- supported `interfaces` and the `browser_route`;
- supported `ingress_modes` and one `default_ingress_mode`;
- `managed_runtimes` for Heartwood-managed inference;
- `scheduler` and `persistent_storage` guidance;
- `credential_backends`;
- permitted `model_sources` and any `platform_isolated_model_sources`;
- `managed_model_connections`; and
- `validation_level`, either `ci` or `ci-and-live-synthetic`.

The gateway serves this record at `GET /project/capabilities`, and every interface uses it to hide unsupported choices.

## Detect the Environment

`PlatformAdapter.detect()` must use deterministic, content-safe evidence such as explicit deployment markers, scheduler identity or Jupyter platform variables.
Do not inspect research data or infer institutional authorization.
The generic adapter is the fallback when no other adapter matches.

## Define Policy and Connections

`PlatformAdapter.default_policy_profile()` returns a deny-by-default `PolicyProfile` with exact model catalog and completion endpoints, capability tiers, action-review modes and credential reference names.
It applies when no deployment manifest is supplied; a manifest's `[policy]` table replaces it.
The built-in generic, Terra and Carina adapters permit `review-all` and `review-changes`.
Expose managed connection metadata without secret values, and never let a user-entered compatible endpoint widen managed-platform policy.

## Gateway Ingress

Choose one of the shared ingress modes instead of adding platform-specific request parsing:

- `direct-loopback` for a client that connects directly to a loopback gateway;
- `jupyter-proxy` for an authenticated local Jupyter proxy that strips one exact external prefix and supplies only validated Jupyter route metadata;
- `trusted-proxy` for a non-loopback deployment with exact proxy source ranges, external origin, base path and forwarded metadata; or
- `identity-proxy` for a gateway behind a reverse proxy that forwards requests without authenticating them, where the gateway verifies one researcher with OpenID Connect.

The deployment supplies route values to `heartwood gateway serve`, which validates them once for the REST API, server-sent events, WebSockets, browser assets and the generated browser base path.
Interface clients do not read Terra, Carina, Jupyter or proxy environment variables.
A trusted proxy removes inbound forwarding and identity headers before setting its own, and its upstream route reaches only the configured gateway bind.

Every request also needs the launch capability.
Either let the researcher open the launch link that `heartwood gateway serve` prints and forward the browser's header and WebSocket authentication frame unchanged, or supply the capability through `--capability-fd` and inject `X-Heartwood-Capability` from the proxy for callers it has authenticated.
Keep the descriptor source and the capability away from the worker and from other users.

In `identity-proxy` mode the gateway issues no launch link and accepts no `--capability-fd`.
The deployment manifest's [`[identity]` table](../reference/configuration.md#identity) names the provider, the gateway's client and the one subject the gateway accepts, in the form the provider issues it.
The proxy keeps the external base path, connects from a declared source range and forwards the `Host` header unchanged.
Register `<public origin><base path>/oauth_callback` as the client's redirect URI at the provider.
The generic adapter offers this mode; an adapter lists it in `ingress_modes` to offer it on another platform.
[Researcher Identity](security.md#researcher-identity) states what the gateway verifies.

## Model Credential Isolation

Leave `platform_isolated_model_sources` empty unless an exact model source uses a transport separated from the operating-system identity that runs OpenHands tools.
Listing a source lets `review-changes` apply in local execution for that source, and the gateway does not check the claim, so add a source only after live synthetic validation on the platform shows that terminal, file-editor and Task tools cannot read its credential.
Qualification is per source: isolating an institutional gateway does not qualify a subscription, hosted-provider or custom endpoint on the same platform.
The selected profile must use the exact model-source identifier and a managed-identity binding; an environment variable or mounted credential file stays application-scrubbed.
Cover restart, every advertised interface, research files, state, inherited process resources, logs, events, browser and notebook outputs and audit exports, and keep model authorization and tool execution independently revocable.

An OpenHands remote workspace isolates the agent environment from its caller, but its agent server holds both the model client and the tools, so it is not a model-only credential boundary.
Prefer a platform-native model proxy or a separately authorized service identity to a Heartwood-managed secret store.

## Protected State and Worker Execution

Managed launch binds the research directory, project UUID, state directory and policy ceiling through the [deployment manifest](../reference/configuration.md#deployment-manifest).
Platform detection and project files cannot establish deployment authority.

A worker adapter confines the complete agent server, model client, tools and subprocesses, and reuses native infrastructure for sandbox creation, mandatory filesystem and network controls, resource limits and credential delivery.
The reference adapter uses OpenShell on a local Docker engine and the native OpenHands remote contracts; its control objects are not portable platform schemas.
Keep the gateway, canonical records, approvals, signing authority and management sockets outside the worker, and supply only selected read-only inputs and verified Skills with separate writable storage.
Bind each attempt to project, session, image, effective policy and input manifest before admitting events; observe admission and revocation through the outside control plane; and keep ownership fenced while creation or deletion is uncertain.
The [worker ownership contract](../architecture/system.md#worker-attempt-ownership) and [native integration checks](../architecture/testing.md#installed-native-integration) define the expected behavior.

## Package the Shared Application

Extend the platform's supported base image with the shared Heartwood payload, preserving its entrypoint, user, Jupyter paths, proxy behavior and required libraries unless the platform requires a change.
Declare image targets in `images/platforms.toml`, parameterize the shared `images/Dockerfile` assembly in `docker-bake.hcl`, and add platform validation scripts only where the inherited environment requires them.
Keep model weights and credentials out of image layers, and publish the manifest media type and architectures the platform accepts.

## Configure Checkpoint Signing

When the platform retains authoritative checkpoints, install one deployment-owned signer registry at `/etc/heartwood/checkpoint-signers.toml`, or set `HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY` to an absolute operator-controlled path.
In containers, mount the registry and public keys read-only and inject any bearer token as a separate secret file.
Publish a production profile as the registry default, keep KMS or HSM credentials and private keys in the signer service, and never bake tokens, keys or project-specific signer choices into an image.
See [Audit Checkpoints and Retention](audit-checkpoints.md).

## Validate Conformance

Add adapter protocol, capability serialization, ingress adversarial, read-only startup, project persistence, model-policy, credential-boundary and interface tests for every advertised surface.
Use a production-derived CI image where the real base cannot run in pull-request CI, then validate the published artifact in a synthetic live environment before setting `validation_level` to `ci-and-live-synthetic`.
