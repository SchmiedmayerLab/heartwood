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
Heartwood supplies the project directory as the agent workspace, and the file editor rejects paths outside the project and inside `.heartwood/`.
Terminal commands are not confined: an approved command can read anything the user can read and can change `.heartwood/` state, including project settings, installed Skills, and session and audit files.
Review commands that touch `.heartwood/` as changes to Heartwood's own controls, and let the operating system, container, or platform enforce stronger isolation when required.

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

The supported GPU runtime no longer depends on `diskcache`; its optional outlines cache uses typed SQLite records and remains disabled by default.
Other runtime and model caches are private to the runtime user and separate from the agent workspace.

Do not enable the unbounded outlines disk cache, share runtime caches between users or trust domains, or place cache directories inside an agent-writable project without a deployment-specific review.

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
Only the selected model credential is scrubbed; other secrets in the Heartwood environment, credential files, and keyring entries remain readable by terminal commands.

A Heartwood-managed local model server listens on a fixed loopback port without authentication.
Other users on the same host can send it requests and, while it is not listening, can take the port and receive prompts in its place.
Run it only on hosts or allocations that other users cannot reach.

[OpenHands Agent Server](https://docs.openhands.dev/sdk/guides/agent-server/overview) and its [remote workspace](https://docs.openhands.dev/sdk/guides/agent-server/cloud-workspace) move the conversation, model client, and tools into a remote agent environment.
That boundary isolates the caller from the agent workspace, but it does not by itself separate model authentication from tools running inside that agent environment.
Heartwood uses the upstream OpenHands credential and model transports without treating a remote workspace as model-only credential isolation.

Use **Review Every Action** for an API key, ChatGPT subscription, mounted secret, or managed identity unless the active platform explicitly reports a qualified platform-isolated boundary.
**Low-Risk Automation** classifies actions from the model's own risk label and a small set of fixed command patterns, so a model that follows injected instructions can label an unsafe command low risk.
Use it only when every file, dataset, and page the agent reads is trusted.
Removing a saved provider credential revokes model access independently of the action policy.
Changing the action policy never broadens model authorization.

Platform isolation is distinct from institutional approval.
A separate service identity or platform proxy can enforce a technical credential boundary, while data eligibility still depends on the provider agreement, institutional controls, and reviewed deployment.

### Gateway Ingress

Heartwood accepts browser and API traffic through one configured ingress mode:

- **Direct loopback** binds to a loopback address by default and rejects forwarding headers.
- **Jupyter proxy** binds to loopback, requires the exact external origin and stripped proxy prefix, and validates the bounded route metadata emitted by `jupyter-server-proxy`.
- **Trusted proxy** accepts traffic only from configured source ranges, requires one complete forwarded client, host, protocol, and prefix set, and can require an exact non-secret proxy identity assertion.

All modes validate the request host, browser origin, WebSocket origin, path, query encoding, and external base path before routing.
Heartwood rejects duplicated security headers, forwarded metadata outside the declared Jupyter or trusted-proxy contract, encoded or traversing paths, wildcard origins, and contradictory prefixes.
HTTP request bodies are bounded to 1 MiB and must be UTF-8.
Static browser responses set a restrictive content security policy, same-origin frame policy, no-referrer policy, MIME-sniffing protection, and a permissions policy that disables camera, geolocation, and microphone access.
The content security policy permits live updates only through the validated browser origin and its corresponding WebSocket origin.

Heartwood does not authenticate end users or terminate public TLS.
The platform proxy must authenticate users, authorize project access, remove untrusted forwarding and identity headers, set the validated values, and restrict network reachability to the configured gateway bind.
A trusted identity assertion is an additional route marker, not a bearer secret or replacement for user authentication.

### Launch Capability

Ingress validation confirms the route, not the caller.
A loopback bind is reachable by every process running as the same operating-system user, including the agent's own terminal, so the gateway additionally requires a process-lifetime launch capability on every API request, server-sent event stream, and WebSocket.
`heartwood gateway serve` generates the secret at startup and prints one launch link.
Opening the link exchanges its one-time token for an HttpOnly, `SameSite=Strict` cookie scoped to the browser-visible gateway path; the token cannot be reused and the secret never appears in page scripts or browser storage.
Automation supplies the same secret through `HEARTWOOD_GATEWAY_CAPABILITY` and presents it in the `X-Heartwood-Capability` header.
The gateway keeps the variable out of the processes it starts, but the operating system still exposes a process's initial environment to other processes of the same user, so the launch link is the stronger route.
A request without the capability receives `HW-INGRESS-003`; static browser assets need no capability because they contain no project data.

Every command accepted through the gateway records the launching principal as its actor.
A caller cannot claim another actor in the command body, and `approval.recorded` carries the actor that approved or denied the action set.

The capability separates the researcher's browser from other operating-system users and from requests that do not carry it.
It is not a boundary against processes running as the same user, including approved agent commands, which can read the gateway's initial environment, the browser profile, and project settings.
Browsers send cookies to every port of a host, so with direct loopback any other local server the browser opens on the same address receives the capability; open only local links you trust while Heartwood runs.
In Jupyter proxy mode, other content served from the notebook origin can use the cookie, as it can use the Jupyter server itself.
The capability does not separate two people who share one operating-system account and does not replace the platform's user authentication.

### Skills and Instructions

A Skill can influence agent behavior and tool selection.
Heartwood therefore accepts reviewed external packages only through deployment-approved TUF roots and verifies current signatures, expiry, rollback state, target hashes, the complete file manifest, declared policy, and OpenHands compatibility before installation.
Approval is bound to the exact complete-tree digest shown to the researcher, installation is atomic, and a signed revocation prevents later activation.
The advanced local-install route accepts source directories only inside the current project and outside reserved `.heartwood` state.

These controls establish source authenticity and package integrity; they do not prove that instructions are safe for every dataset or task.
Review the description, tools, network and data-access requirements, dataset types, source, and permission summary before installation, and treat instructions embedded in project files or other external content as potentially untrusted.

Repository review, project installation, and controlled-data approval are independent decisions.
Only the deployment-owned system Skill registry can approve an exact verified digest for controlled data.
Catalog metadata and local unreviewed packages cannot grant that status.

See [Skill Trust and Distribution](../architecture/skills.md).

### Audit Data

The audit log is hash-chained and export is scrubbed, but operational metadata can still be sensitive.
Store, retain, share, and delete audit artifacts under the same reviewed records policy as the surrounding project.
Normal project exports remain replaceable by the project owner.
Where authoritative evidence is required, create a signed checkpoint outside the project and verify it against a public key obtained through a separate trusted channel.
The signature authenticates the supplied checkpoint statement and audit content; it does not enforce retention, establish institutional approval, or prove that no events were removed before checkpoint creation.

Managed deployments keep the checkpoint signer registry, trusted public keys, service credentials, and private signing keys outside every agent project.
Project state can select only a profile already approved by that registry.
Heartwood verifies a remote signature and its pinned signer identity before publishing a checkpoint.
A production signing service must independently authorize the requested deployment and retention claims, use a principal unavailable to agent tools, and keep KMS/HSM credentials outside the Heartwood process.
An owner-only bearer-token file remains accessible to other processes running as that owner unless the platform supplies a stronger sandbox.
The bundled local signer is an authenticated loopback-only development and offline fallback; its owner-only file permissions protect against other operating-system users but not compromise of the same user account.
See [Audit Checkpoints and Retention](audit-checkpoints.md).

## Deployment Security Checklist

Confirm each control before researchers add project data, and again after each update:

- authenticate every user before they reach the execution environment;
- isolate projects and users with platform permissions or containers;
- keep the gateway on loopback or behind one configured authenticated proxy boundary;
- deny network egress except reviewed model and package endpoints;
- mount controlled inputs read-only when feasible;
- keep provider secrets in a keyring, mounted secret, or managed identity;
- keep production checkpoint keys in an independently authorized KMS/HSM-backed signer service;
- use **Review Every Action** until a deployment-specific risk policy is reviewed, and apply the [reviewed assistant](reviewed-assistant.md) controls;
- keep the launch link and any supplied gateway capability private to the researcher who started Heartwood;
- pin release artifacts, images, model revisions, and Skill versions;
- collect content-minimized operational logs outside project outputs;
- validate backup, retention, deletion, and incident-response procedures; and
- perform synthetic end-to-end validation before controlled-data enablement.

## Claims and Evidence

Describe a deployment as suitable for a data class only when the institution can point to the relevant platform controls, agreements, security review, and validation evidence.
An implemented or CI-tested Heartwood route is not equivalent to live deployment approval.
