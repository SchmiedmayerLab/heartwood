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

In local execution, OpenHands tools execute with the permissions of the Heartwood process.
Heartwood supplies the project directory as the local agent workspace, and the file editor rejects paths outside the project and inside `.heartwood/`.
Local terminal commands are not confined: an approved command can read anything the user can read and can change `.heartwood/` state, including project settings, installed Skills, and session and audit files.
Review commands that touch local `.heartwood/` as changes to Heartwood's own controls.

Managed native execution requires an explicit operator-owned deployment binding and qualified worker dependencies.
The complete worker runs behind the sandbox boundary, receives selected read-only input and verified Skill projections, and keeps its writable workspace, home and temporary files inside the sandbox.
Canonical state, deployment authority, provider credentials, management sockets and signing material remain outside its mounts and process identity.
The gateway independently checks attempt identity, admits untrusted responses and retains approval authority; the sandbox and outside services enforce filesystem, process and network restrictions.
An uncertain revocation fences the attempt and retains writer ownership until reconciliation establishes that its resources are gone.
This path does not silently replace local execution and does not establish sensitive-data readiness merely by starting a worker.
See the [platform isolation requirements](platform-contract.md#agent-isolation) and [native qualification limits](../architecture/testing.md#installed-native-integration).

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

The supported GPU runtime does not depend on `diskcache`; its optional outlines cache uses typed SQLite records and remains disabled by default.
Other runtime and model caches are private to the runtime user and separate from the agent workspace.

Do not enable the unbounded outlines disk cache, share runtime caches between users or trust domains, or place cache directories inside an agent-writable project without a deployment-specific review.

### Credentials

Provider credentials are excluded from project configuration, browser storage, command arguments, durable session events, logs, and audit exports by design.
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
For local execution, other secrets in the Heartwood environment, credential files, and keyring entries remain subject to the same-identity limitations.
The native worker instead starts with an explicit environment allowlist and receives only the outside provider transport's placeholder; ordinary remote execution without that transport does not separate model credentials from agent tools.

A Heartwood-managed local model server uses a private Unix socket in an owner-only temporary directory outside the research project.
The launcher and model clients validate directory ancestry, ownership, permissions and socket identity; managed requests have no TCP fallback.
The logical model URL in policy and configuration identifies the private transport, not a listening TCP port.
The launcher stops the interaction if its runtime exits, and llama.cpp slot inspection is disabled.
These controls separate operating-system users; they do not isolate processes sharing the launcher's identity or protect against a privileged host administrator.
Sandboxed workers cannot use this local socket: they require an independently authorized outside inference route, with no socket or credential mount into the worker.

[OpenHands Agent Server](https://docs.openhands.dev/sdk/guides/agent-server/overview) and its [remote workspace](https://docs.openhands.dev/sdk/guides/agent-server/cloud-workspace) move the conversation, model client, and tools into a remote agent environment.
That boundary isolates the caller from the agent workspace, but it does not by itself separate model authentication from tools running inside that agent environment.
Heartwood uses the upstream OpenHands credential and model transports without treating a remote workspace as model-only credential isolation.

Use **Review Every Action** for an API key, ChatGPT subscription, mounted secret, or managed identity unless the active platform explicitly reports a qualified platform-isolated boundary.
**Low-Risk Automation** classifies actions from the model's own risk label and a small set of fixed command patterns, so a model that follows injected instructions can label an unsafe command low risk.
Use it only when every file, dataset, and page the agent reads is trusted.
Removing a credential from Heartwood's saved settings does not invalidate copies held by running processes or revoke the key at its issuer.
For sandbox provider access, require acknowledged outside detachment or verified worker revocation; neither undoes requests already forwarded upstream.
Changing the action policy never broadens model authorization.

Credential rotation is separate from revocation.
After an ordinary static provider update, a running process can retain a reference to the previous value; launch a new process to receive the updated reference.
For deployments that need value-only rotation without replacing the worker, the provider profile can explicitly enable OpenShell's `stable_placeholder` capability before launch.
Qualify that mode against the deployed gateway and supervisor, wait for the update's readiness receipt, and independently verify that acknowledged detachment prevents retained references from reaching the provider.
Updating a stored value alone does not establish that the previous upstream key is safe to retire.

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
A loopback bind alone does not authenticate a caller; local same-identity tools can reach it.
The gateway requires a process-lifetime launch capability on every API request, server-sent event stream and WebSocket, including deployments that also deny direct worker access through network isolation.
`heartwood gateway serve` generates the secret at startup and prints one launch link only after owning a listening socket.
Gateway request access logging is disabled so launch tokens do not enter access logs.
The launch token is carried in a URL fragment, which the browser removes before exchanging it through `POST /launch`; it is never sent in an HTTP URL.
The response supplies a gateway-only capability, stored in tab-scoped `sessionStorage` under the gateway base path.
The browser sends it explicitly in `X-Heartwood-Capability` for API and server-sent event requests, and in the first WebSocket authentication frame.
Requests stay within the configured origin and base path and reject redirects; the gateway sets no capability cookie.
The launch token works once; reloads retain access in the same tab while the gateway runs.
An unauthenticated WebSocket receives no project events and must authenticate within five seconds using a text frame of at most 8192 bytes.
Automation supplies the same secret through `--capability-fd` and presents it in the `X-Heartwood-Capability` header.
The gateway consumes and closes that inherited file or pipe descriptor before serving requests; environment delivery is rejected.
Keep any source file outside worker access, and hand off the descriptor exclusively with no other readers.
The descriptor payload is limited to 4096 bytes and pipes must reach EOF within five seconds.
A request without the capability receives `HW-INGRESS-003`; static browser assets need no capability because they contain no project data.

Every command accepted through the gateway records the launching principal as its actor.
A caller cannot claim another actor in the command body, and `approval.recorded` carries the actor that approved or denied the action set.

The capability separates the researcher's browser from other operating-system users and from requests that do not carry it.
It is not a boundary against processes running as the same user, including approved agent commands, which can read the browser profile and project settings or inspect same-user processes.
A server on another port cannot read the tab’s origin-scoped storage or receive the capability through an automatically attached cookie.
Scripts executing on the same origin can read browser storage; a base-path storage key is routing separation, not a security boundary against same-origin code.
In Jupyter proxy mode, notebook content served from the same origin remains trusted, as it is for the Jupyter server itself.
Use separate authenticated origins for mutually untrusted projects and keep untrusted scripts outside the gateway origin.
The capability does not separate two people who share one operating-system account and does not replace the platform's user authentication.

### Skills and Instructions

A Skill can influence agent behavior and tool selection.
Heartwood therefore accepts reviewed external packages only through deployment-approved TUF roots and verifies current signatures, expiry, rollback state, target hashes, the complete file manifest, declared policy, and OpenHands compatibility before installation.
Approval is bound to the exact complete-tree digest shown to the researcher, installation is atomic, and a signed revocation prevents later activation.
Sessions load each installed Skill only from its exact verified directory, and unverified content beside an active installed Skill stops Skill listing and starting agent sessions until it is removed.
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
The bundled local signer is a development and offline fallback using an owner-only Unix socket and bearer authentication.
The client validates socket ownership, permissions and its protected directory before transmitting the token.
These controls protect against other operating-system users, but not compromise of the same user account.
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
- use **Review Every Action** until a deployment-specific risk policy is reviewed;
- keep the launch link and any supplied gateway capability private to the researcher who started Heartwood;
- pin release artifacts, images, model revisions, and Skill versions;
- collect content-minimized operational logs outside project outputs;
- validate backup, retention, deletion, and incident-response procedures; and
- perform synthetic end-to-end validation before controlled-data enablement.

## Claims and Evidence

Describe a deployment as suitable for a data class only when the institution can point to the relevant platform controls, agreements, security review, and validation evidence.
An implemented or CI-tested Heartwood route is not equivalent to live deployment approval.
