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

The browser treats agent responses as untrusted GitHub-Flavored Markdown.
It removes raw HTML and unsafe link protocols, does not fetch model-provided images, makes invisible control characters explicit, normalizes response headings beneath the page heading, bounds displayed content, and escapes plain technical fields.
External links require an explicit user action and omit referrer information.

Confirm data eligibility for the exact endpoint, account, model, and deployment before use.

### Runtime Caches

The supported GPU launcher disables vLLM's optional on-disk outlines cache because its entries are deserialized by the inference process.
Other runtime and model caches are private to the runtime user and separate from the agent workspace.

Do not enable the outlines disk cache, share runtime caches between users or trust domains, or place cache directories inside an agent-writable project.

### Credentials

Raw credentials are excluded from project configuration, browser storage, command arguments, durable session events, logs, and audit exports by design.
Heartwood resolves process values, operator bindings, optional system-keyring entries, or platform identity only for named provider calls.
Provider failures are reduced to OpenHands' typed failure category while the exception is still in process.
Raw provider exception text is replaced before OpenHands runtime persistence and is not retained in Heartwood retry logs, session events, or audit data.

Project-scoped keyring persistence is explicit and available only where a functional system credential store exists.
Custom compatible-service tokens remain process-only.
For **Sign in with ChatGPT**, OpenHands owns the user-level OAuth credential, refresh, and Codex transport; Heartwood stores only a non-secret subscription reference in project policy.
Signing in does not make a ChatGPT account suitable for controlled data.

#### Model Credential Isolation

Heartwood reports one of three effective boundaries for the active model route:

| Boundary | Guarantee | Action Policy |
|---|---|---|
| Credential-free | No model credential is placed in the Heartwood process, as with an unauthenticated local model server | Platform policy may permit Low-Risk Automation |
| Application-scrubbed | Heartwood excludes the credential from tool inputs, project state, terminal subprocess environments, interfaces, events, logs, and audits | Review Every Action is required |
| Platform-isolated | A separately authorized model transport keeps the credential outside the operating-system identity that runs agent tools, with live synthetic qualification for that platform | Platform policy may permit Low-Risk Automation |

Application scrubbing is not process isolation.
OpenHands' local model client and coding tools run under the same Heartwood operating-system identity, so environment filtering cannot prevent every same-identity memory, process, or inherited-resource access path.
The built-in workstation/container, Terra, and Carina adapters therefore do not claim platform-isolated model credentials.

[OpenHands Agent Server](https://docs.openhands.dev/sdk/guides/agent-server/overview) and its [remote workspace](https://docs.openhands.dev/sdk/guides/agent-server/cloud-workspace) move the conversation, model client, and tools into a remote agent environment.
That boundary isolates the caller from the agent workspace, but it does not by itself separate model authentication from tools running inside that agent environment.
Heartwood uses the upstream OpenHands credential and model transports without treating a remote workspace as model-only credential isolation.

Use **Review Every Action** for an API key, ChatGPT subscription, mounted secret, or managed identity unless the active platform explicitly reports a qualified platform-isolated boundary.
Removing a saved provider credential revokes model access independently of the action policy.
Changing the action policy never broadens model authorization.

Platform isolation is distinct from institutional approval.
A separate service identity or platform proxy can enforce a technical credential boundary, while data eligibility still depends on the provider agreement, institutional controls, and reviewed deployment.

### Gateway Ingress

Heartwood accepts browser and API traffic through one configured ingress mode:

- **Direct loopback** binds to a loopback address by default and rejects forwarding headers.
- **Jupyter proxy** binds to loopback, requires the exact external origin and stripped proxy prefix, accepts requests only from the local Jupyter proxy, and validates the bounded route metadata emitted by `jupyter-server-proxy`.
- **Trusted proxy** accepts traffic only from configured source ranges, requires one complete forwarded client, host, protocol, and prefix set, and can require an exact non-secret proxy identity assertion.

All modes validate the request host, browser origin, WebSocket origin, path, query encoding, and external base path before routing.
Heartwood rejects duplicated security headers, forwarded metadata outside the declared Jupyter or trusted-proxy contract, encoded or traversing paths, wildcard origins, and contradictory prefixes.
HTTP request bodies are bounded to 1 MiB and must be UTF-8.
Static browser responses set a restrictive content security policy, same-origin frame policy, no-referrer policy, MIME-sniffing protection, and a permissions policy that disables camera, geolocation, and microphone access.
The content security policy permits live updates only through the validated browser origin and its corresponding WebSocket origin.

Heartwood does not authenticate end users or terminate public TLS.
The platform proxy must authenticate users, authorize project access, remove untrusted forwarding and identity headers, set the validated values, and restrict network reachability to the configured gateway bind.
A trusted identity assertion is an additional route marker, not a bearer secret or replacement for user authentication.

### Skills and Instructions

A Skill can influence agent behavior and tool selection.
Structural validation and provenance records do not make third-party instructions trustworthy.

Review Skill source and declared tools, install only through an approved path, and treat instructions embedded in project files or external content as potentially untrusted.

### Audit Data

The audit log is hash-chained and export is scrubbed, but operational metadata can still be sensitive.
Store, retain, share, and delete audit artifacts under the same reviewed records policy as the surrounding project.
Normal project exports remain replaceable by the project owner.
Where authoritative evidence is required, create a signed checkpoint outside the project and verify it against a public key obtained through a separate trusted channel.
The signature authenticates the supplied checkpoint statement and audit content; it does not enforce retention, establish institutional approval, or prove that no events were removed before checkpoint creation.
See [Audit Checkpoints and Retention](audit-checkpoints.md).

## Recommended Controls

- authenticate every user before they reach the execution environment;
- isolate projects and users with platform permissions or containers;
- keep the gateway on loopback or behind one configured authenticated proxy boundary;
- deny network egress except reviewed model and package endpoints;
- mount controlled inputs read-only when feasible;
- keep provider secrets in a keyring, mounted secret, or managed identity;
- use **Review Every Action** until a deployment-specific risk policy is reviewed;
- pin release artifacts, images, model revisions, and Skill versions;
- collect content-minimized operational logs outside project outputs;
- validate backup, retention, deletion, and incident-response procedures; and
- perform synthetic end-to-end validation before controlled-data enablement.

## Claims and Evidence

Describe a deployment as suitable for a data class only when the institution can point to the relevant platform controls, agreements, security review, and validation evidence.
An implemented or CI-tested Heartwood route is not equivalent to live deployment approval.
