<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Security and Controlled Data

Heartwood controls what an agent may do inside a research environment; the environment's identity, network, storage and data-governance controls remain the authoritative boundary.
No Heartwood configuration establishes institutional approval, HIPAA compliance, or readiness to process protected health information or other sensitive data.

## Threat Model

Heartwood treats the following as untrusted input: model output, tool output, research files, Skill content, external service responses, and, in an isolated deployment, everything the worker reports.
It trusts the operator who writes the [deployment manifest](index.md#deployment-manifest), the platform proxy that authenticates researchers, the gateway process and the host it runs on.
The threats it addresses are an agent that proposes harmful actions, content that injects instructions, other operating-system users and network clients reaching the gateway, and a worker that tries to claim authority it was not given.
A compromised host, platform proxy, model provider or sandbox control plane is outside what Heartwood can detect.

There are two execution shapes.
State placement is a separate choice: a deployment manifest without a `[worker]` table moves canonical state outside the research directory but keeps local execution.

### Local Execution

OpenHands tools run in the gateway process, as the operating-system user who started Heartwood.
The file editor rejects paths outside the project and inside `.heartwood/`.
Terminal commands are not confined: an approved command can read anything that user can read, including `.heartwood/` settings, installed Skills, session records, audit files and browser profiles, and can inspect same-user processes.

Local execution protects against other operating-system users and against callers without the [launch capability](#launch-capability).
It does not protect against the agent's own approved commands, other processes running as the same user, or a privileged administrator.
For that reason no review mode allows a terminal command automatically in local execution, and every reduced mode also requires the [credential gate](#model-credential-isolation).

### Isolated Worker

A deployment manifest with a `[worker]` table runs the complete OpenHands agent server, model client, tools and subprocesses as a worker inside an OpenShell sandbox on a local Docker engine.
The gateway, canonical state, deployment authority, provider credentials, signer material and management sockets stay outside the worker's mounts and process identity.
The worker receives only the inputs selected in the manifest and verified Skills, both read-only, plus a private writable workspace, home and temporary directory.

The isolated worker passes native qualification against a patched OpenShell gateway that exists only in the qualification build; see [Testing and Evidence](../architecture/testing.md#installed-native-integration).
Heartwood distributes no production sandbox gateway, and checks a deployment's sandbox gateway only by the deletion capabilities it reports, as described in [Gateway Behavior Behind Native Qualification](platform-requirements.md#gateway-behavior-behind-native-qualification).

## Sandbox Enforcement

The sandbox and the surrounding infrastructure enforce everything that constrains code running inside the worker:

- filesystem access limited to the projected volumes, with inputs and Skills mounted read-only;
- a non-root process identity with `no-new-privileges` and every Linux capability dropped;
- Docker `network=none` for the worker container, with network access only through the sandbox's deny-by-default policy and inference transport;
- CPU and memory limits from the manifest and a process limit;
- substitution of the real provider credential outside the worker, which holds only an `openshell:resolve:env:` placeholder; and
- deletion of the sandbox when the gateway revokes the attempt.

Heartwood sets no expiry on the provider credential behind that placeholder.
Credential lifetime, rotation and detachment are the operator's OpenShell provider configuration; native qualification checks only that its own fixture's expired credential is refused.

The worker's attempt key, `SESSION_API_KEY`, authenticates only requests to that worker's own agent server.
Agent commands do not inherit it, but they run as the worker user that started the agent server, so code in the worker may be able to read the key from that process's start-up environment under `/proc`; native qualification records whether it can.
The key grants no authority beyond what code in the worker already has.

Heartwood does not inspect commands the worker runs.
In [`review-outside-effects`](#action-review), these controls are the only limit on what an automatically allowed command can do inside the worker.

## Gateway Enforcement

The gateway enforces everything that decides authority, independently of anything the worker reports:

- the deployment manifest's paths, ownership and policy ceiling, before any store opens;
- both deletion capabilities reported for the sandbox gateway's one compute driver, which must be the implementation the manifest's `driver` selects, and stops with `HW-WORKER-001` otherwise; this relies on the gateway's self-report and does not verify its behavior, so the operator must still confirm the [gateway behavior behind native qualification](platform-requirements.md#gateway-behavior-behind-native-qualification);
- outside inspection of the worker container, which rejects an unexpected image, a network other than `none`, privileged mode, any retained capability, a missing `no-new-privileges`, a shared host or container process, IPC, user or cgroup namespace, a device, a bind mount, mounts that expose withheld host paths, missing CPU and memory limits or process limit, and any security option other than `no-new-privileges`, AppArmor `docker-default`, or an SELinux `container_t` type with individual categories;
- admission of the sandbox's accepted policy hash and process generation against `sandbox_policy_sha256`;
- worker attempts bound to project, session, image, policy and input-manifest identity, journaled and fenced so that only one attempt can write a session;
- worker responses admitted only when every conversation setting equals what the gateway sent, so a worker cannot select the agent, model client, security analyzer or secret source the gateway constructs;
- a 120-second deadline per worker request and fixed size limits per response, with a fenced attempt on violation;
- action review, outside authorization and audit records, which a worker message cannot create or settle; and
- revocation of a fenced attempt and removal of its volumes, and of attempts left by an earlier gateway process at the next `gateway serve --deployment` start.

The [Kubernetes admission](../architecture/system.md#kubernetes-worker-admission) inspects the pods the OpenShell Kubernetes driver creates, also from outside the worker, and lists its accepted values next to the Docker values.
It differs from Docker inspection in these points:

- A pod has no network mode.
  Admission confirms that the driver's two NetworkPolicies exist with the driver's rules.
  It refuses any other NetworkPolicy of the namespace that selects the workload pod and has a rule.
  Cluster-wide and plugin-specific policies are not read.
  The Kubernetes API does not report whether the cluster's network plugin enforces the policies.
- The process limit is the kubelet's `podPidsLimit`, a node setting that no pod field reports.
- The cgroup namespace is not reported.
  Container runtimes on cgroup version 2 give each container a private one.
- The driver sets no AppArmor profile and no SELinux options, so the node's defaults apply.
  Admission refuses a pod that sets either, and the pod does not report the profile in effect.
  The `restricted` Pod Security level is the namespace's label; exemptions configured in the API server and pods created before the label was set are not visible to admission.
- A persistent volume claim can be backed by a host path.
  Only the cluster-scoped PersistentVolume shows that, and admission compares the claim names in the pod.
- CPU and memory limits are compared with the manifest for the `agent` container only.
  The driver sets none on the init containers and the supervisor pod.
- Admission reads the pods once.
  A principal with `pods/ephemeralcontainers`, `pods/exec`, or `pods/attach` in the namespace, or one that can patch pods, can change or enter a pod afterwards.
- [Staging](../architecture/system.md#kubernetes-worker-staging) runs in a pod, which cannot disable its own network.
  The gateway requires NetworkPolicies that select the staging pod, cover ingress and egress, and allow nothing, and it relies on the cluster's network plugin to enforce them.
  Only `networking.k8s.io` NetworkPolicies of the namespace are read; a cluster-wide or plugin-specific policy that allows traffic is not visible to the gateway.
- The staging pod runs as user `10002` with every capability dropped, where the Docker preparation container runs as user `0` with `FOWNER`.
  The staged files belong to user `10002`, so the worker, user `10001`, has no write permission on them.
- Admission does not compare container commands, node placement, or tolerations.
- The attempt key is a plain environment value in the workload pod and in its Sandbox resource.
  Every principal that can read them can read the key, as with `docker inspect` on Docker.

The platform must therefore provide an enforcing network plugin, a kubelet process limit, nodes on cgroup version 2 with a default AppArmor or SELinux confinement for containers, and access rules for the sandbox namespace.
For staging it must also provide, in the sandbox namespace, a service account `hw-staging` without permissions, a storage class `heartwood-attempt`, and NetworkPolicies that deny the staging pods all traffic.

An uncertain revocation keeps the attempt fenced and the session's writer ownership retained until outside reconciliation observes that its resources are gone.
After a recorded revocation, the next task can start a replacement worker in a new sandbox; it shares no credential, placeholder, conversation memory or workspace file with the revoked worker.
A gateway restarted with a changed worker image in its manifest continues a settled session in a replacement worker launched from that image; a session the earlier worker left waiting for review, running or paused is refused and continues in a new session.
Rotating the provider credential in the sandbox gateway does not update a worker that is already attached: its next model request fails and Heartwood records the provider failure, so nothing runs with a stale route.
After the gateway restarts and revokes that worker, the session continues in a replacement worker that attaches the rotated credential.
The system architecture describes [attempt ownership](../architecture/system.md#worker-attempt-ownership) and the [response limits](../architecture/system.md#budgets-and-deadlines).

## Action Review

The agent loop pauses before every registered action set.
The gateway then either waits for the researcher or records its own decision under the actor `gateway` and one named rule.
Model risk ratings and model-written summaries are not inputs to that decision.

| Mode | Allowed automatically | Applies when | Residual exposure |
|---|---|---|---|
| `review-all` | Nothing | Always | None beyond the researcher's own decisions |
| `review-changes` | A set containing only file views of paths the gateway resolved inside the project (rule `read.file-view`), or only task-tracker updates (rule `plan.task-tracker`) | The model route is credential-free or platform-isolated, or the session runs in an isolated worker | Viewed file content reaches the model route without a per-file decision |
| `review-outside-effects` | A set containing only file views, task-tracker updates, file edits and terminal commands (rule `sandbox.contained`) | The session runs in an admitted, active worker attempt with a live lease | Code runs unreviewed on mounted inputs, can alter or delete the worker workspace, and can send content to the admitted model endpoint |

An action set with any other tool, or with an action that the deterministic OpenHands policy-rail and pattern analyzers rate high or cannot assess, waits for the researcher in every mode.
Outside operations and result releases are never allowed automatically; see [Outside Authorization](#outside-authorization).
A researcher approval never overrides deployment policy, and no mode requires an outside approval before every local operation until an executor that mediates each operation separately from the worker is qualified.
[Actions and Audit History](../use/actions-audit.md#review-modes) describes how researchers select a mode and read automatic decisions.

### Deployment Ceiling

`allowed_action_review_modes` in the manifest's `[policy]` table lists every mode the deployment permits and must include `review-all`.
It can contain only modes the platform policy permits, plus `review-outside-effects` when the manifest has a `[worker]` section; any other entry stops the launch with `HW-DEPLOY-002`.
When it is omitted, those same modes are permitted.
A session in an isolated worker with no selection then uses `review-outside-effects`, so in-worker commands run without a researcher decision.
List the modes explicitly, and list only `review-all` until a mode has passed the [action-review evaluation](../architecture/testing.md#action-review-evaluation) for the deployment.

The gateway reads the ceiling from the manifest each time it loads the project.
A project policy can list fewer modes; the modes in force are those both lists permit.
A stored selection that the ceiling no longer permits is replaced by `review-all` when the project loads, recorded in each session before its next model dispatch and reported as `HW-SETUP-003`.
At decision time the ceiling is applied again, and any mode outside it, unknown, or not applicable to the session resolves to `review-all`.

Without a deployment manifest, the built-in workstation and container, Terra and Carina policies permit `review-all` and `review-changes`.
State is then inside the research directory, so the gateway keeps the researcher's selection in process memory and ignores any review mode in `.heartwood/config.toml`.
`--review-mode` on `heartwood` or `heartwood gateway serve` makes the same selection at start and stops with the reason when the ceiling, worker or credential gate refuses it.

### Deployment Policy

The manifest's `[policy]` table narrows the detected platform adapter's policy; it never replaces it.
At launch, Heartwood refuses a manifest whose policy permits a review mode, capability tier, credential backend, credential name or model endpoint the platform does not, or a lower aggregate count floor, with `HW-DEPLOY-002`.
A platform that offers **Other compatible service** already lets its own setup add any compatible endpoint and credential name, so on the workstation/container and Terra adapters a manifest may name such a route; on Carina it may use only the loopback and Stanford AI API Gateway routes.
`allowed_credential_backends` limits where provider credentials may come from: without `process`, interfaces cannot enter a credential and only bindings in the trusted launch environment resolve, and without `keyring` nothing is read from or saved to a system credential store.
`allowed_checkpoint_signer_profiles` limits the signer registry's profiles a project may select; a list with one profile fixes it, and a project cannot select or store another.
An omitted `allowed_action_review_modes`, `allowed_credential_backends` or `allowed_checkpoint_signer_profiles` would permit anything, so it follows the platform policy, and removing such a line widens the policy.
The optional `[requirements]` table makes a missing control a launch failure with `HW-DEPLOY-003`: `isolated_worker = true` requires a `[worker]` section, the separately mediated executor that review of registered tool calls does not replace, and `checkpoint_evidence = true` requires an `[evidence]` section in `required` mode.

`heartwood doctor --deployment`, the browser's project details and the notebook bridge's `project_readiness()` report the same effective policy, its source, the manifest digest and whether the manifest requires or configures an isolated worker and checkpoint evidence; `render_policy()` displays it in a notebook.
The gateway rereads the manifest before each command that can continue agent work.
A change that only narrows the policy applies before that command, and a change to the policy or to the review mode in force, whether it comes from the manifest or from a selection in any interface, is recorded as a `policy.decision.recorded` event before the session's next model dispatch.
Any other manifest change, including a change to `allowed_credential_backends` or the removal of a restricting line, stops dispatch with `HW-DEPLOY-004` until the gateway restarts, so loosening always requires the operator to restart under the changed manifest.

### Failure Behavior

The gateway asks the researcher whenever it cannot establish that a rule applies: the session is paused or the researcher's last command paused, denied or was refused; the selection, ceiling or credential boundary cannot be read; the worker attempt is missing, fenced, revoked or being replaced; the action set is not fully journaled; or the session has an unresolved outcome.
A decision is computed once, when the review request is journaled, and bound to the pending set's identity.
A set whose request was journaled before a restart or a mode change waits for the researcher.
An automatic decision that cannot be completed, for example because required checkpoint evidence is unavailable, is withdrawn: the journal records that it was not applied, and the set waits for the researcher.

## Outside Authorization

An outside operation is a request to a platform service prepared through a trusted embedding's adapter; a result release discloses that operation's result to the worker and its model route.
`heartwood gateway serve` installs the adapter for [research MCP servers](platform-contract.md#research-mcp-servers) when the deployment manifest lists MCP destinations and a worker.
Other services need an embedding launcher that supplies an adapter, as the native qualification fixture does.
Each needs its own researcher decision bound to the exact proposal digest, including the request, input digests, destination, worker identity, policy revision and a five-minute validity interval.
No review mode decides either one.

Before submission or delivery, the gateway rechecks the live worker attempt, current policy, validity interval and input contents.
The decision and dispatch intent are journaled before the service is contacted, and with managed evidence, retained outside the gateway first.
An approved operation is sent only by an exact retry of its own approval command; reads, reconciliation and restarts never send it.
A Heartwood approval is necessary, never sufficient: the destination service authenticates the caller and enforces its own authorization.
For a [platform job](platform-contract.md#platform-job-operations), the platform's proposal is untrusted text shown as the platform's, and a cancellation reaches the service with the target identifier from the gateway's record of an operation the session sent, never from the worker's text.
The [Platform Contract](platform-contract.md#outside-operation-admission) defines the embedding interface.

## Credentials

Provider and research service credentials are excluded from project configuration, browser storage, command arguments, durable session events, logs and audit exports.
Heartwood resolves process values, operator bindings, optional system-keyring entries or platform identity only for named provider calls.
Provider failures are reduced to a typed category before persistence, so raw provider exception text is not retained.
Custom compatible-service tokens remain process-only; project-scoped keyring persistence is explicit and requires a working system credential store.
For **Sign in with ChatGPT**, OpenHands owns the user-level OAuth credential and transport, and Heartwood stores only a non-secret subscription reference.

### Model Credential Isolation

The credential gate reports one boundary for the active model route:

| Boundary | Meaning | Reduced review outside an isolated worker |
|---|---|---|
| Credential-free | No model credential is placed in the Heartwood process, as with an unauthenticated local model server | Permitted |
| Application-scrubbed | The credential is in the Heartwood process but excluded from tool inputs, project state, terminal environments, interfaces, events, logs and audits | Refused; `review-all` applies |
| Platform-isolated | A separately authorized transport keeps the credential outside the identity that runs agent tools, with live synthetic qualification for that platform and model source | Permitted |

Application scrubbing is not process isolation: the local model client and tools share one operating-system identity.
No built-in adapter claims a platform-isolated model source.
An isolated worker never holds the provider credential, so `review-outside-effects` does not depend on this gate.

Local terminals receive an explicit environment instead of the gateway's: executable and virtual-environment paths, locale and terminal settings, temporary directories, selected CUDA and Slurm variables, thread limits and public certificate paths.
`TERMINAL_ENVIRONMENT_NAMES` in the gateway defines the exact names, and `HEARTWOOD_TERMINAL_ENV_ALLOWLIST` in the trusted launch environment adds comma-separated names.
Configured provider credential names and the gateway capability stay excluded even when listed; proxy variables are excluded unless explicitly listed.
The allowlist does not stop a local command from reading same-user process memory, credential files, keyrings or cloud metadata services.

A Heartwood-managed local model server listens on an owner-only Unix socket in an owner-only directory outside the project, with no TCP fallback.
That protects it from other operating-system users, not from same-user processes; an isolated worker cannot reach it.

Removing a credential from Heartwood settings does not revoke it at its issuer or in running processes.
Rotating a sandbox provider credential is not revocation: confirm acknowledged detachment or worker revocation, neither of which recalls requests already forwarded.
OpenShell's `stable_placeholder` provider capability allows value-only rotation without replacing the worker; qualify it against the deployed gateway before relying on it.

### Repository Secret Scan

The **Secret Scan** workflow runs a pinned, checksum-verified gitleaks release and reports findings in redacted form.
Pull-request and main-branch validation scan the history reachable from the commit they validate.
A scheduled run every day at 10:17 UTC, and every manual run of the workflow, scans the complete history of every branch and tag, including branches without a pull request.
That run fails when its checkout lacks a branch or tag the repository advertises, so a partial checkout cannot pass as a complete scan.
A finding fails the workflow without printing the matched value.
Removing a detected secret from history and rotating it remain manual incident steps; a scan does not show that a value was never exposed.

## Gateway Ingress

The gateway accepts traffic through one configured ingress mode:

- `direct-loopback` binds to loopback and rejects forwarding headers;
- `jupyter-proxy` binds to loopback, requires the exact external origin and stripped prefix, and validates the route metadata of `jupyter-server-proxy`;
- `trusted-proxy` accepts only configured source ranges with one complete forwarded client, host, protocol and prefix set, and can require an exact non-secret identity assertion header; and
- `identity-proxy` binds to a non-loopback address, accepts only configured source ranges, keeps the external base path, and verifies the researcher as described in [Researcher Identity](#researcher-identity).

Every mode validates host, origin, WebSocket origin, path, query encoding and base path, and rejects duplicated security headers, undeclared forwarding metadata, encoded or traversing paths and wildcard origins.
Request bodies are limited to 1 MiB of UTF-8.
Browser responses set a restrictive content security policy, same-origin framing, no referrer, MIME-sniffing protection, and a permissions policy that disables camera, geolocation and microphone.
In `identity-proxy` mode the forwarded client, host, protocol, port and prefix headers are optional.
The host, the first protocol value, the first port value and the prefix must match the external origin and base path when present; `X-Real-IP` and `X-Forwarded-Server` are accepted and unused.
A top-level `GET` navigation to the gateway's root, `identity/login` or `oauth_callback` may arrive from another site in that mode, because a sign-in returns through the identity provider.
Heartwood does not terminate public TLS.
Outside `identity-proxy` mode Heartwood does not authenticate end users; the platform proxy must do so and strip client-supplied forwarding and identity headers.

## Launch Capability

Every API request, server-sent event stream and WebSocket requires a process-lifetime capability.
`heartwood gateway serve` prints one launch link after it owns a listening socket; the browser removes the fragment token and exchanges it once through `POST /launch` for a capability kept in tab-scoped `sessionStorage` under the gateway base path.
The browser sends it in `X-Heartwood-Capability`, or in the first WebSocket frame within five seconds and 8192 bytes; no cookie is set and redirects are rejected.
Automation supplies the capability through `--capability-fd`, at most 4096 bytes, which the gateway reads and closes; environment delivery is rejected.
Access logging is disabled, and a request without the capability receives `HW-INGRESS-003`.

The capability separates the researcher's browser from other operating-system users and from requests that lack it.
It does not separate the researcher from same-user processes, from scripts on the same origin, or from another person sharing the account.
Every accepted command records the launching principal, `human`, as its actor; a command cannot claim another actor, and the actor `gateway` is reserved for automatic review decisions.
Use separate authenticated origins for mutually untrusted projects; in Jupyter proxy mode, notebook content on the same origin is trusted.

## Researcher Identity

With `identity-proxy` ingress the gateway is a confidential OpenID Connect client bound to one subject by the manifest's [`[identity]` table](../reference/configuration.md#identity).
It replaces the launch link: every API request, server-sent event stream and WebSocket needs a sign-in capability or an access token, and a request with neither receives `HW-INGRESS-004`.

A browser that opens the gateway's URL is redirected to `identity/login` and from there to the provider's authorization endpoint with a `state`, a `nonce` and a PKCE challenge.
The gateway seals these values into the `heartwood-sign-in` cookie with a key that exists only in its process, and keeps no record of a sign-in in progress.
The provider returns the browser to `oauth_callback`, which accepts the request only with that cookie, unaltered, at most ten minutes old, with a `state` equal to the sealed one and not used before.
The gateway then redeems the code with its client secret, verifies the ID token and removes the cookie.
The gateway accepts RS256 and ES256 signatures made with the provider's published keys, and requires the configured issuer, an audience that names its client, an unexpired lifetime, the `nonce` of this sign-in and the bound subject.
An access token that is a JSON Web Token must carry the same issuer and subject and name the client as audience or authorized party.
The browser then receives a one-time launch token in the URL fragment and exchanges it at `POST /launch` for a capability of that sign-in, which it sends as in the [launch capability](#launch-capability) flow.
A sign-in ends when its refresh token expires, when the provider reports that time, and otherwise when its access token expires.

Other clients send `Authorization: Bearer` with an access token of the same issuer whose `aud` contains the gateway's client and whose `sub` is the bound subject.
A token that carries a `nonce`, `at_hash` or `c_hash` claim, or a `typ` claim of `ID`, is an identity token and is refused as a bearer token.
Every accepted command records the verified subject as its actor.

The gateway keeps the researcher's access and refresh tokens, the client secret, authorization codes and capabilities in process memory.
They are not written to project files, protected state, events, audit exports or logs, and they are not passed to agent tools or to the model.
Without a [`[worker]`](../reference/configuration.md#deployment-manifest) table, agent tools run as the gateway's operating-system user and can read the client secret file and, where the operating system permits it, the gateway's memory.
A deployment with `[identity]` should therefore also configure a worker.

- One gateway accepts one subject; a token of any other person is refused.
- A restart of the gateway ends every sign-in, and the researcher signs in again.
- The gateway holds at most eight sign-ins and drops the oldest beyond that.
- A sign-in completes only in the browser that started it; a second sign-in started in the same browser replaces the first.
- The `heartwood-login` cookie that the callback sets for one minute authorizes nothing; it lets the page load once so that it can exchange the launch token.
- The launch token in the URL fragment and the capability in the browser's session storage are readable by every script on the gateway's origin.
- Gateways of several researchers that share one origin under different paths are therefore separated only while nothing else on that origin serves pages a researcher controls.
- Static responses forbid framing, so another page cannot embed the gateway; a script on the same origin can still open the gateway in another window and read its launch token.
- With an `http` public origin, the authorization code, the launch token, the capability, bearer tokens that clients present and the cookies cross the network between the client and the proxy unencrypted, and the cookies carry no `Secure` attribute.
- An event stream or WebSocket that was opened with a sign-in's capability keeps delivering events after that sign-in ends, until the connection closes.
- An unsigned token, a token signed with a shared secret, and a token signed with a key the provider does not publish are refused.
- When the provider cannot be reached or answers with an unusable document, the request receives `HW-INGRESS-005` without the provider's text, and the gateway asks the provider for its discovery document or keys again after five seconds at the earliest.

## Model Content

Prompts, selected project content, tool results and summaries go to the active model route.
Policy denies unlisted endpoints, but infrastructure egress controls and provider agreements are authoritative; confirm data eligibility for the exact endpoint, account, model and deployment.

The browser renders agent responses as untrusted GitHub-Flavored Markdown: it removes raw HTML and unsafe link protocols, does not fetch model-provided images, shows invisible control characters, and opens external links only on explicit action without a referrer.

The GPU runtime's optional outlines cache is disabled by default and does not use `diskcache`.
Do not enable unbounded runtime caches, share them between users, or place them inside an agent-writable project.

## Skills and Instructions

Reviewed external Skill packages install only from deployment-approved TUF roots, after verification of signatures, expiry, rollback state, target hashes, the complete file manifest, declared policy and OpenHands compatibility.
Approval binds the exact tree digest, installation is atomic, and a signed revocation prevents later activation.
Only the deployment-owned system Skill registry can approve a digest for controlled data.
The agent receives each verified active Skill directory individually, never a parent directory that OpenHands would scan for more Skills.
Content placed beside an installed Skill in the project store, including another Skill directory or an interrupted installation, is not loaded; Heartwood names it in the Skill list, in `heartwood doctor` and in the gateway log.
Release-bundled Skills must match the complete-tree digests pinned in the Heartwood release; an edited or missing bundled Skill stops Skill use, and unpinned entries beside them are reported and never loaded.
Container and worker images install that directory and the pin root-owned.
Source checkouts and native installations leave both writable by the runtime user, so the pin detects drift there but cannot stop that account, or a terminal command it approves, from rewriting the installation.
Verification establishes source and integrity, not that instructions are safe for a dataset; see [Skill Trust and Distribution](../architecture/skills.md).

## Audit Data

The audit log is hash-chained and its export is scrubbed, but operational metadata can still be sensitive; store and delete it under the project's records policy.
A signed checkpoint authenticates one export against a key obtained through a separate channel.
It does not enforce retention or prove that no events were removed before it was created.
Managed evidence additionally verifies each checkpoint against independently retained history, and the signer, not the gateway, publishes checkpoint ordinals.
See [Audit Checkpoints and Retention](audit-checkpoints.md).

## Security Limits

Native qualification runs synthetic and benign workloads, and no deployment of Heartwood is qualified for sensitive or regulated data.
Synthetic qualification, CI validation and a running sandbox are evidence for an institutional review, not institutional approval.
Review modes do not constrain a compromised isolated worker, which can act inside its sandbox without an approval.
A released outside result reaches the model as a user-role message; the envelope that labels it is not an instruction boundary.
Outside `identity-proxy` ingress Heartwood does not verify individual identities.
Heartwood does not attribute commands to individual people behind one gateway or replace platform authorization.
Heartwood provides no quarantine, steward-approved output release, encrypted execution storage or records-system retention enforcement.

## Deployment Security Checklist

Confirm each control before researchers add project data, and again after each update:

- authenticate every user before they reach the execution environment, and isolate projects and users with platform permissions;
- keep the gateway on loopback, behind one configured authenticated proxy, or in `identity-proxy` mode behind a proxy with a declared source range, and keep the launch link and capability private to the researcher;
- deny network egress except reviewed model and package endpoints;
- keep provider secrets in a keyring, mounted secret or managed identity, and production checkpoint keys in an independently authorized signer;
- list `allowed_action_review_modes` explicitly, with only the modes evaluated for the deployment;
- for an isolated worker, confirm that the sandbox gateway provides the [behavior native qualification depends on](platform-requirements.md#gateway-behavior-behind-native-qualification) before declaring its version;
- pin release artifacts, images, model revisions and Skill versions;
- collect content-minimized operational logs outside project outputs;
- validate backup, retention, deletion and incident-response procedures; and
- complete synthetic end-to-end validation before any controlled-data decision.

Describe a deployment as suitable for a data class only when the institution can point to the platform controls, agreements, security review and validation evidence for it.
