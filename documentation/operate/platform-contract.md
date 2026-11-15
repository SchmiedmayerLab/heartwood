<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Platform Contract

A research platform can embed Heartwood as its research interaction layer while keeping authority over identity, compute, storage, inference and spending.
This contract states what each side provides, how the contract is versioned, and how to check an integration against it.
[Platform API and Access Requirements](platform-requirements.md) turns it into an access request for a specific platform.

Each part is marked with one status:

- **Current**: implemented, tested, and exported as a current artifact.
- **Draft**: an exported schema with offline checks; Heartwood does not send or consume the message at runtime.
- **Unavailable**: neither implemented nor exported.

A schema describes a message; it does not enable a service, authorize a request, or establish that a deployment is qualified.

## Scope and Status

| Part | Status |
|---|---|
| [Hosting](#hosting) the published artifact with persistent project storage | Current |
| [Access and identity](#access-and-identity) through an authenticated proxy and the launch capability | Current for one launching principal per gateway; per-user identity verification unavailable |
| [Deployment policy](#deployment-policy) bound by the operator deployment manifest | Current |
| [Inference](#inference) through built-in and operator-configured routes | Current; connection-manifest startup unavailable; model metadata draft |
| [Agent isolation](#agent-isolation) of the complete worker | Current for the OpenShell Docker reference adapter; qualified only against the qualification-only sandbox gateway build |
| [Controlled data](#controlled-data) controls | Selected read-only inputs current; quarantine and steward-approved release unavailable |
| [Audit and records](#audit-and-records) | Signed checkpoints and deployment-bound evidence current; signer and records-service qualification is the platform's |
| [Platform services](#platform-services) over MCP, outside operations, launch context and budgets | Draft; the content-bound [outside operation](#outside-operation-admission) authorization is current, but no outside-service adapter ships and built-in launchers install none |
| Hosted per-project gateways with verified OpenID Connect identity | Unavailable |
| Realtime voice | Unavailable |

## Responsibilities

| Area | Heartwood | Platform |
|---|---|---|
| Researcher identity | Records the launching principal as the actor of every accepted command and ignores actors claimed in requests | Authenticates the person, authorizes project access, ends the session |
| Gateway access | Requires the launch capability on every request and validates the declared ingress route | Routes only authenticated users to the gateway, strips client-supplied forwarding and identity headers, terminates TLS, delivers the capability |
| Research session | Owns canonical session records, action review, outside authorization and interface projections | Keeps the gateway, research files, protected state and network route available |
| Policy | Evaluates model routes, review modes and credential references against the deployment ceiling | Defines the ceiling and enforces filesystem, process and network restrictions |
| Inference | Selects an authorized route; keeps credentials out of model context, tool inputs, events and exports | Operates endpoints, enforces account, model and tenant authorization, data-use agreements and spend limits |
| Agent isolation | Admits, fences and revokes worker attempts; admits worker responses against what it sent | Provides the sandbox, its control plane, and credential substitution outside the worker |
| Platform services | Defines draft messages and offline checks; binds outside operations to researcher decisions | Authorizes every call; owns scheduling, quotas, cost, entitlements and infrastructure policy |
| Project and billing context | Binds the research directory and state through the deployment manifest | Selects the platform project and billing or grant attribution |
| Audit and provenance | Writes content-minimized audit and experiment records and requests signed checkpoints | Authorizes the signer, retains records immutably, keeps its own operation audit |
| Controlled data | Makes no compliance claim | Decides data eligibility, agreements, egress, monitoring and incident response |

Platform authentication does not replace gateway controls, and a Heartwood approval never grants platform authority.

## Interfaces

| Direction | Interface | Authority |
|---|---|---|
| Researcher interface → gateway | Generated gateway OpenAPI, server-sent events and WebSocket | Gateway validates ingress, capability, commands and approvals |
| Worker → inference transport → provider | Existing provider API and model identifiers | Transport holds the credential and routes; provider authorizes account, model and tenant |
| Trusted adapter → platform service | Existing platform APIs; optionally MCP under the draft profile | Platform authenticates each call and enforces project, dataset, compute and billing permissions |
| Launcher → sandbox infrastructure | Existing infrastructure control API | Operator owns admission, policy, runtime identity and revocation |
| Gateway → signer and records service | Checkpoint-signing HTTPS interface and S3-compatible object storage | Signer authorizes publication; records service enforces scoped access and retention |

The contract reuses existing standards and adds only what Heartwood needs:

| Area | Upstream definition | Heartwood addition |
|---|---|---|
| Inference | Provider APIs supported by the SDK, including Chat Completions and Responses | Route eligibility, model metadata declarations |
| Platform tools | MCP discovery, calls, results, errors and capabilities | Required annotations, policy checks, review of side effects |
| Jobs, workflows and data | Native scheduler and storage APIs; GA4GH TES, WES and DRS where supported | References linking native resources to proposals, estimates and audit records |
| Cost | FOCUS definitions | Workload assumptions, estimate coverage and validity, budget observations |
| Asynchronous results | MCP Tasks extension, revision 2026-07-28 | Estimate payload carried in task results |

Upstream definitions stay authoritative for their own messages, streaming, errors and lifecycle; the bundle pins their revisions instead of copying them.

## Platform Responsibilities

### Hosting

Required:

- Run the published image or native release without replacing its entrypoint, user or bundled browser assets.
- Provide persistent research storage and separate state storage with native advisory locks; object-store and file-transfer mounts are not session stores unless qualified.
- Keep model weights, credentials and deployment secrets out of image layers and the project directory.
- Keep the owner-only Unix sockets and directories of a Heartwood-managed model server and of a local checkpoint signer outside worker access.

Recommended:

- Verify the release's build attestations before admitting an artifact, and back up project storage across compute replacement.

Optional:

- Provide Jupyter where the platform offers the [notebook bridge](../use/notebooks.md).

Releases carry build attestations; separate artifact signatures, software bills of materials and third-party notice bundles are unavailable for release images and native assets.

| Topology | Runs the gateway | Authenticates the person | Status |
|---|---|---|---|
| Workstation or container | The researcher | The operating system and the launch link | Current |
| Platform workspace | The platform, in the researcher's workspace | The platform proxy | Current for the Terra terminal and notebook and the Stanford Carina terminal, CI validated |
| Hosted per-project service | The platform, one gateway per researcher and project | The institution's identity provider, verified by Heartwood | Unavailable |

Gateway shutdown does not stop external jobs or storage charges.

### Access and Identity

Required:

- Authenticate every user before traffic reaches the gateway, and restrict the gateway's network route to the platform proxy.
- Remove client-supplied forwarding and identity headers before setting the proxy's own values.
- Deliver the launch capability, either by letting the researcher open the one-time launch link, or by injecting `X-Heartwood-Capability` for requests the platform has already authenticated.
- Keep the capability away from the agent workspace, the worker, other users and logs.

Recommended:

- Run one gateway per researcher and project; Heartwood records one launching principal per gateway and does not distinguish people who share it.
- Serve mutually untrusted projects on separate origins; base paths on one origin do not isolate browser storage.

A proxy that injects the capability authenticates on the researcher's behalf and must do so only for the user who owns that gateway.
For example, a proxy at `https://research.example.org` that strips the prefix `/heartwood/project-a` and connects from `10.0.0.2` starts the gateway with a secret from its secret manager:

```bash
heartwood gateway serve --capability-fd 3 3</run/secrets/heartwood-capability \
  --ingress-mode trusted-proxy \
  --host 10.0.0.5 \
  --port 8767 \
  --public-origin https://research.example.org \
  --base-path /heartwood/project-a \
  --trusted-proxy-source 10.0.0.2/32 \
  --proxy-strips-prefix
```

The proxy then adds `X-Heartwood-Capability` with the same secret to each authenticated request.
[Gateway Ingress](security.md#gateway-ingress) and [Launch Capability](security.md#launch-capability) state the exact validation rules, and the [command reference](../reference/cli.md#gateway-serve) lists every serve option.

### Deployment Policy

Required:

- Bind each managed project through an operator-owned [deployment manifest](index.md#deployment-manifest) outside research files and state.
- List exact model and catalog endpoints, credential references and permitted review modes, with egress denied by default.
- Enforce filesystem, process and network restrictions outside Heartwood; policy validation cannot constrain a compromised process.

Recommended:

- List only `review-all` in `allowed_action_review_modes` until each further mode has been evaluated for the platform; the default when the key is omitted permits the platform's modes, plus `review-outside-effects` with a worker.

Optional:

- Publish approved Skill sources through the deployment-owned registry at `/etc/heartwood/skill-sources.toml`; only that registry can approve a Skill for controlled data.

Project settings can narrow the ceiling but cannot widen endpoints, credentials, capability tiers, review modes or permitted data destinations, remove a deployment's data-destination list, lower the aggregate-count floor, disable default-deny egress or change the platform identifier.
Without a manifest, the platform adapter's built-in policy applies and state stays in the research directory.

### Inference

Required:

- An OpenAI-compatible Chat Completions endpoint with streaming and native tool calls for compatible services; provider-specific routes keep their SDK-supported protocols.
- A model list endpoint that returns only the models the authenticated identity may use.
- Platform-held credentials, through a managed identity, a process-only variable or a mounted file outside the project.
- A statement of which data classes each route may receive; compatibility does not establish eligibility.
- A declaration of each connection's [data handling](#route-data-handling): where the complete route sends model requests and who governs retention of the content it receives.
- Distinguishable failures for rejected credentials, exhausted quota, rate limits, invalid model configuration and temporary unavailability.

Required for isolated workers:

- An outside inference transport that substitutes the credential, provider-side enforcement of the approved account, model set and tenant even when several models share one hostname, and an operator-configured credential lifetime and rotation; Heartwood sets no expiry on it.

Recommended:

- The Responses API where the model supports it; Heartwood selects the protocol per model from the OpenHands model capabilities.

Optional:

- Compute on which Heartwood [runs a managed model](../models/run-with-heartwood.md), and an approved process to [import verified model bundles](../models/offline.md).

Heartwood discovers models through a policy-checked catalog endpoint and never implements a second provider proxy.
The `heartwood.model-connections.v1` schema describes deployment-owned connections, but startup does not load a connection manifest, and the OpenHands model table does not recognize platform model aliases.
The draft model-metadata supplement declares, per model identity:

| Field | Meaning | Level |
|---|---|---|
| Model identifier | The exact value sent in each request | Required |
| Protocols | Chat Completions, Responses or both | Required |
| Context and token limits | Context window and input and output limits; missing values stay unknown | Recommended |
| Tool calling | Whether native tool calls are supported | Required |
| Data classes | Classes the route may receive under the platform's agreements | Required |
| Deprecation | Whether the model is being withdrawn, so no model is silently substituted | Recommended |

#### Route Data Handling

A connection's `data_handling` descriptor declares the `destination` of model requests, its `retention_authority` and a `route` description, such as an institution's gateway followed by the providers it routes to.

| Destination | Meaning |
|---|---|
| `same-compute` | The inference service runs in the compute environment that runs the Heartwood project |
| `institution-managed` | An institution-operated service, such as an inference gateway, receives the requests |
| `provider` | The model provider's service receives the requests directly |
| `operator-defined` | A service whose handling its operator defines outside these classes |

Retention authority is `deployment`, `institution`, `provider` or `service-operator`.
Built-in connections supply descriptors; a platform supplies one for each connection through the `data_handling` field of the `heartwood.model-connections.v1` manifest, and a connection without one is undeclared.
The descriptor is a declaration by the connection's owner; Heartwood does not verify provider behavior, retention or data eligibility from it, and does not infer it from endpoint URLs.
Describe the complete route: an outside credential-substituting transport such as an OpenShell proxy does not change the declared recipient.

Setup responses expose the descriptor and a shared summary for every connection and for the active route.
Each `model_call.decision.recorded` event, which precedes the model work it authorizes, records the descriptor of the route the session uses, and the scrubbed audit export retains it without credentials.
Each worker attempt records the descriptor of the route it was launched for with its other launch identities.
A policy's `allowed_data_destinations` restricts routes to the listed destinations: selection and each model call fail closed when a route's descriptor is missing or names another destination.
A project policy can narrow the list but cannot remove or widen a deployment's list.

### Agent Isolation

For an isolated worker, the platform provides:

Required:

- A sandbox with mandatory filesystem and process restrictions, resource limits, deny-by-default network enforcement, and a control plane the worker cannot reach.
- Read-only projections of selected inputs and verified Skills, a private writable workspace, and no research-directory, protected-state or Docker-socket mount.
- Provider credentials, signing authority, launch capabilities, management sockets and peer-worker state outside worker access.
- Authenticated admission bound to project, session, attempt and runtime identity, with a changed generation or configuration fencing the old attempt.
- Revocation observed outside the worker before replacement or writer release, with reconciliation instead of duplicate execution when creation or deletion is uncertain.
- A sandbox gateway that provides the [deletion behavior native qualification depends on](platform-requirements.md#gateway-behavior-behind-native-qualification), with its version declared in `gateway_versions`.

The reference adapter uses OpenShell on a local Docker engine with the native OpenHands remote conversation, workspace and event contracts.
OpenShell names, policy objects and control credentials belong to that adapter, not to portable platform messages; another adapter must keep the same authority, lifecycle and evidence properties on its own infrastructure.
Worker-reported events and output are untrusted: they cannot create approvals, authorize platform operations or prove that the worker has stopped.
[Sandbox Enforcement](security.md#sandbox-enforcement) and [Gateway Enforcement](security.md#gateway-enforcement) divide these controls between the two sides.

### Controlled Data

Required:

- Decide data eligibility for every model route, dataset and interface.
- Provide read-only input snapshots, enforced egress denial and ephemeral, encrypted execution storage for controlled work.
- Keep outputs in quarantine until an authorized data steward approves an exact release manifest.

Heartwood provides the selected-input transfer: the manifest's `[worker.inputs]` binds each file's path, size and SHA-256, at most 128 files, 16 MiB per file and 64 MiB in total.
Staging rereads and verifies the bytes, rejects symbolic links, multiply linked and nonregular files, excludes `.git` and `.heartwood`, and uses a private directory outside the project that is removed when staging ends.
File digests establish content identity, not authorization to disclose the content.
Heartwood provides no quarantine, encrypted storage or steward-approved release, and is not a controlled-data boundary on its own; see [Security and Controlled Data](security.md).

### Audit and Records

Recommended:

- A deployment-owned checkpoint signer, installed through `/etc/heartwood/checkpoint-signers.toml` or `HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY`, with keys no agent can use.
- Retention, immutability and deletion enforcement for exported audit and experiment records.

Required for managed evidence:

- An independently authorized signer that retains each checkpoint before returning it, and a versioned, retention-locked records service with separate identities for the gateway and the signer.

Required for platform services:

- An operation audit log joinable to Heartwood's audit by operation reference.

The signer interface is `POST /v1/checkpoints/sign`, accepting `heartwood.audit-checkpoint-sign-request.v1` and returning `heartwood.audit-checkpoint-signature.v1` over HTTPS.
[Audit Checkpoints and Retention](audit-checkpoints.md) defines the registry, the evidence table and the storage permissions.

### Platform Services

The service, operation, task and budget messages are draft.
Heartwood disables MCP servers in its OpenHands configuration and advertises no platform service.

#### Service Discovery

Required:

- Expose services over MCP Streamable HTTP with a stable server identity.
- Describe each tool's purpose, input and output schemas, and required permission; read access never implies permission to allocate resources or attach data.
- Annotate each tool with explicit boolean `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` values.
- Enforce identity, quotas and dataset entitlements on every call, independently of Heartwood's approval.
- Accept delegated or platform-held credentials that never reach model context, tool environments or exports.

Annotations are hints from the server, not authorization, and a changed tool set requires a new review.

#### Operations That Change Platform State

A tool that allocates compute, submits work, attaches data or starts an application is an operation.

Required:

- Before approval, return a proposal with resource limits, expected duration, estimated cost when available, data scope and destination.
- Accept a caller-supplied operation identifier, scoped to the authorized project and service and bound to the exact proposal; a repeat with the same contents is the same operation, and different contents under the same identifier are a conflict.
- An estimate or preparation call that persists data, incurs charges or reserves resources follows the normal review and authorization path.
- Report status by operation identifier, so an uncertain outcome is reconciled rather than resubmitted.
- Report cancellation as the observed result, without promising rollback.
- Return a reference that joins the platform's audit record to Heartwood's session audit without copying payloads.

```mermaid
stateDiagram-v2
    [*] --> Submitted
    Submitted --> Denied: platform refuses
    Submitted --> Queued: platform accepts
    Submitted --> Running
    Submitted --> Completed: immediate result
    Submitted --> Failed
    Submitted --> Unknown: response lost
    Queued --> Running
    Queued --> Cancelled
    Queued --> Failed
    Unknown --> Denied: status reconciled
    Unknown --> Queued: status reconciled
    Unknown --> Running: status reconciled
    Unknown --> Completed: status reconciled
    Unknown --> Failed: status reconciled
    Unknown --> Cancelled: status reconciled
    Running --> Completed
    Running --> Failed
    Running --> Cancelled: cancellation observed
    Denied --> [*]
    Completed --> [*]
    Failed --> [*]
    Cancelled --> [*]
```

These states are what Heartwood shows; keep native backend states and references alongside them.
`Unknown` means Heartwood has no authoritative observation, not that nothing happened, and a cancelled operation can leave completed work and incurred charges.

#### Asynchronous Results

Pending results use the [MCP Tasks extension, revision 2026-07-28](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks), exported unchanged with its pinned commit, checksum and license.

- The server advertises `io.modelcontextprotocol/tasks` under `capabilities.extensions`, and each task-enabled `tools/call` opts in through `params._meta["io.modelcontextprotocol/clientCapabilities"].extensions`.
- A call returns either the immediate tool result or a flat `resultType: "task"` handle.
- `tasks/get` returns status and, when completed, the original tool result, whose `structuredContent` uses the same estimate schema as the immediate path.
- `tasks/update` supplies requested input and does not bypass review or authorization.
- `tasks/cancel` acknowledges a request; observe the task, backend operation and spending separately afterward.

A completed task can carry a tool result with `isError: true`.
Expired or missing task state requires reconciliation by operation identifier before resubmission.
`mcp-tasks.SOURCE.txt` records where the profile interprets differences between upstream prose and examples.

#### Launch Context, Budgets and Estimates

The platform owns project selection, billing attribution, spending thresholds, reservations, accounting and enforcement.
Heartwood compares estimates the platform produces; it does not price work, monitor spending or cap bills.
A draft `heartwood.platform-launch-context.v1` document carries the platform-owned binding:

```json
{
  "schema_version": "heartwood.platform-launch-context.v1",
  "project_context_id": "launch-synthetic-1",
  "platform_id": "synthetic-platform",
  "project_id": "research-project-a",
  "billing_attribution_ref": "binding-a",
  "authority_ref": "platform-authority",
  "policy_ref": "policy-v1",
  "issued_at": "2026-10-17T10:00:00Z"
}
```

Keep it outside agent-writable state; possession of a copy does not authenticate its issuer.
It is separate from the deployment manifest, which binds only the research directory, state and policy.

`fixtures/synthetic/platform-contract/` contains a complete exchange for an exploration, a batch and an inference workload, plus `mcp-tasks.json` with immediate and deferred estimates and a cancellation followed by continued backend work.
Read each exchange in this order:

1. `prediction`: quantities, units, assumptions and project-local evidence for one plan revision.
2. `estimates`: platform prices for that prediction; `scope` distinguishes a plan from an element quote, and `coverage` states how complete the cost information is.
3. `operation_sets`: exact native-request references, hard limits and embedded estimate snapshots for review.
4. `researcher_decisions`: the decision bound to the proposal revision and document hash; the platform's own decision is in `authorizations`.
5. `operations` and `budgets`: separately observed execution and accounting state, including unknown or stale values.

Rules for these messages:

- An estimate total already includes components marked `included_in_total`; separate estimates for the same work are alternatives, not additive charges.
- Amounts are decimal strings; `null` is unknown and `"0"` is an explicit zero; denominations must match before comparison, and Heartwood performs no conversion.
- The proposal digest is SHA-256 over the exact UTF-8 document bytes, not a canonical JSON form; reserializing requires a new binding and decision.
- `reviewed_operation_set()` rejects duplicate JSON keys, and `verify_reviewed_operation_set()` checks a stored envelope against the original bytes.
- An estimate, a researcher approval, a platform authorization and a spending reservation are distinct states, and approving a plan does not authorize each operation in it.

Price and cost fields reuse FOCUS definitions from the SKU Price proposal at revision `005a30566a754db4e0f346d20c7063daba579080` and the estimation-features proposal at revision `a2f9f93cb618ab490b90098d0456dfcdb813c5e7`; these are draft references, not a claim of FOCUS 1.5 conformance.

| Requirement | FOCUS definition or boundary |
|---|---|
| Rate amount and basis | `UnitPrice` and `UnitPriceType`, preserving decimal precision and distinguishing list, base and contracted rates |
| Measurement scale | `PricingUnit`; a token count is not a million-token pricing quantity |
| Denomination | `PricingCurrency`, distinguishing currency or credits from resource quantities |
| Price identity and period | The SKU Price dataset's provider, SKU, contract and price-period context |
| Price eligibility | `SkuPriceEligibility`, which describes pricing, not authority to use a resource |
| Workload, coverage and decisions | Heartwood interaction records, not FOCUS columns |
| Observation time and estimate expiry | Platform interaction records, not the price's effective period |

## Heartwood Responsibilities

### Gateway API

`gateway.openapi.json` defines every REST operation, the launch-token exchange, server-sent events and, under `x-websocket`, the WebSocket transport, generated from the typed contracts.
The [Gateway API reference](../reference/gateway-api.md) explains command envelopes, approval targets, retries and recovery.

- HTTP requests carry `X-Heartwood-Capability`; capability cookies are not accepted.
- A browser WebSocket sends `{"type": "authenticate", "capability": "…"}` as its first frame within five seconds; a trusted proxy may inject the header instead.
- Clients resume streams with the `after` query cursor; `Last-Event-ID` replay is not implemented.
- During shutdown, new REST and event-stream requests receive `503`, WebSockets close with code `1012`, and the terminal exits with code `75`; reconnect to the next gateway with the last cursor.
- Project maintenance or a conflicting owner returns `409` or closes WebSockets with `1012`; it is not permission to start a second writer.
- Session command payloads are JSON objects validated by their command handlers.

Proxies must support unbuffered streaming and WebSocket upgrades.
A successful HTTP exchange does not establish that an asynchronous research action completed.

### Outside Operation Admission

A trusted Python embedding can connect platform services to the research session.
It supplies a session-scoped `ExternalActionAdapter` through `SessionGateway(external_actions_factory=...)` and the SDK `ClientToolSpec` definitions through `native_worker_plan_factory(client_tools=...)`.
The worker registers those tools; their acknowledgement proposes an operation and does not execute it.
No production outside-service adapter ships with Heartwood: `heartwood gateway serve` and the other built-in launchers install none, and the gateway has no HTTP route for registering adapters or creating proposals.
The interface is exercised only by embedding launchers such as the native qualification fixture.

To offer outside operations, a platform or embedding launcher must provide:

- a launcher that constructs `SessionGateway` with protected state from a deployment manifest and an owned native worker;
- an `ExternalActionAdapter` implementation for its services, holding the service credentials outside the worker;
- the exact `ClientToolSpec` definitions for the proposal tools, whose canonical schemas are bound into the worker input identity;
- trusted input storage for `read_input` that does not follow worker-selected paths; and
- a destination service that authenticates the adapter and enforces entitlement, project scope, expiry and idempotency itself.

The adapter's contract:

- `prepare` resolves the native request and immutable input references with no external effect;
- `check_policy` and `check_result_policy` raise when current policy or the actor's entitlement forbids dispatch or disclosure;
- `read_input` returns bounded bytes from trusted storage, which the gateway verifies before use;
- `submit` sends once and independently enforces actor, project, live attempt, policy, expiry and content binding, so a repeated operation identifier never creates a second effect;
- `observe` queries an existing operation without submitting; and
- `prepare_result` returns sanitized JSON through the service's own authorization, without credentials.

`SessionService.propose_external_action` turns a retained worker proposal into a review with a gateway-issued operation identifier, the complete request, input digests, destination, worker identity, policy revision and a five-minute validity interval.
The approve and deny commands bind the full proposal digest.
Immediately before submission the gateway rechecks the live worker attempt, project, policy, actor entitlement, validity and input contents, and submits the verified bytes without reopening worker paths.

The decision and submission intent are journaled before the service is contacted; with managed evidence, the authorization is retained outside first, even in buffered mode.
An approved operation that could not be sent is sent only by an exact retry of its own approval command; status reads, reconciliation and automatic decisions never send it.
Denial withdraws an authorization that has not been submitted; an undispatched authorization that reaches the end of its validity interval is recorded as expired and never sent.
After a review expires, `propose_external_action` for the same worker proposal records the expiry and issues a new identifier and review.
`reconcile_external_actions` queries the existing operation after an interruption and never resubmits; an observation must match project, service, operation and proposal digest.
Denying an already submitted operation does not cancel it.

The audit records operation identifiers, digests, decisions and status without request or input payloads.
The service must independently authenticate the caller and enforce entitlement, project scope, attempt revocation, policy, expiry and idempotency, and the worker must have no other route to it.

### Result Release to the Worker

Approving an outside operation does not authorize disclosing its result.
`SessionService.propose_external_result` reads the result of a terminal, observed operation from the current worker attempt and opens a separate review that binds the exact result, the observation, worker identity, policy revision, declared model identifier and route, and a fresh five-minute validity interval.
Results are limited to 64 KiB including the provenance envelope, and ambiguous JSON is rejected.
Approval requires current disclosure policy, the live worker lease and an idle conversation.

The release reaches the model as a user-role message whose JSON envelope labels it as an outside result; that label is not an instruction boundary.
There is one open release review per operation, denial is final for it, and an interrupted delivery is never retried automatically.

### Failure States

| Condition | The platform returns | Heartwood shows |
|---|---|---|
| Capability not offered | No advertised support | The choice is hidden in every interface |
| Missing or invalid launch capability | Not applicable | [HW-INGRESS-003](../reference/troubleshooting.md#gateway-ingress) |
| Request does not match the declared route | Not applicable | [HW-INGRESS-002](../reference/troubleshooting.md#gateway-ingress) |
| Sandbox gateway version not declared | Its `Health` version | [HW-WORKER-001](../reference/troubleshooting.md#managed-workers) |
| Expired or rejected model credential | An authentication error | [HW-AGENT-008](../reference/troubleshooting.md#agent-runtime) |
| Exhausted quota or budget | A quota error | HW-AGENT-009 |
| Rate limit | A rate-limit error with a retry interval when known | HW-AGENT-010 |
| Denied or misconfigured model | A model or authorization error | HW-AGENT-011 |
| Service temporarily unavailable | An unavailability error | HW-AGENT-012 |

[Diagnostics and Troubleshooting](../reference/troubleshooting.md) gives each code's recovery steps.
Platform-operation failures have no runtime presentation because the operation profile is draft.

### Security Properties

Both sides must preserve these properties:

1. Model output, tool output, service responses and dataset content cannot approve an action, widen policy or grant platform permissions.
2. A Heartwood approval is necessary, never sufficient; the platform authorizes every call.
3. Credentials never appear in project configuration, model context, tool environments, events, logs or exports.
4. Service results, tool descriptions, annotations and platform responses are untrusted, bounded input.
5. A timeout or lost response is resolved by status, never by resubmission.
6. Egress is denied by default, and the platform network is the authoritative egress control.

## Versioning

The contract bundle follows [Semantic Versioning 2.0.0](https://semver.org/) independently of the application; this release's bundle is `0.2.0`, recorded as `contract_version` in `contract.json`.

- Compatibility covers message structure, validation rules and documented behavior.
- Before `1.0.0`, a minor version may break compatibility and a patch preserves it; from `1.0.0`, incompatible changes increment the major version.
- Published versions are immutable.
- An added field is not compatible with a consumer that rejects unknown fields.
- Draft integrations pin the exact bundle version and reassess each change.
- Document identifiers such as `heartwood.platform-estimate.v1` name format families, not independent releases; the bundle version and artifact digest identify a revision.
- Upstream specifications keep their own versions, such as the date-based MCP Tasks revision.

Each artifact in `contract.json` carries a `maturity` of `current`, `draft` or `upstream`.

The shared data-class vocabulary is `public`, `internal`, `confidential` and `restricted`.
The classes are not an ordered permission hierarchy, an allowance for one class on one route does not extend to other classes or purposes, and unknown eligibility stays unknown.

| Class | Handling meaning |
|---|---|
| `public` | Approved by its owner for public release |
| `internal` | Nonpublic, for ordinary use within an authorized organization or project |
| `confidential` | Sensitive, restricted to authorized recipients and purposes |
| `restricted` | Requires additional legal, contractual, institutional or owner controls |

## Contract Artifacts

| Artifact | Defines | Maturity |
|---|---|---|
| `contract.json` | Bundle version, compatibility rules, artifact digests, pinned design sources | Index |
| `gateway.openapi.json` | Gateway routes, bodies, streams and the capability security scheme | Current |
| `capabilities.schema.json` | The platform capability manifest served at `GET /project/capabilities` | Current |
| `policy-profile.schema.json` | `heartwood.policy-profile.v1`, the policy in the deployment manifest, including permitted data destinations | Current |
| `model-connections.schema.json` | `heartwood.model-connections.v1`, including data-handling descriptors; startup does not load it | Current |
| `usage-summary.schema.json` | `heartwood.project-usage-summary.v1`, content-free project counts and outcomes served at `GET /project/usage-summary` | Current |
| `model-metadata.schema.json` | Model protocol, capability and eligibility declarations | Draft |
| `data-classification.schema.json` | The data-class vocabulary | Draft |
| `mcp-service.schema.json`, `mcp-tool-annotations.schema.json` | Deployment service references and required tool annotations | Draft |
| `mcp-tasks.schema.json` | MCP Tasks revision 2026-07-28, with `mcp-tasks.LICENSE.txt` and `mcp-tasks.SOURCE.txt` | Upstream |
| `launch-context.schema.json`, `budget-observation.schema.json` | Platform project binding and budget observations | Draft |
| `workload-prediction.schema.json`, `platform-estimate.schema.json` | Predictions and platform estimates with coverage, units and validity | Draft |
| `operation-set.schema.json`, `researcher-decision.schema.json`, `platform-authorization.schema.json`, `operation-observation.schema.json`, `planning-overview.schema.json` | Reviewed proposals, decisions, platform authorization and observed operations | Draft |

The bundle does not export the deployment manifest or the worker profile; [Configuration and State](../reference/configuration.md#deployment-manifest) specifies them.

### Generate and Review the Current Artifacts

From a development checkout of the release commit, export the bundle into a new directory:

```bash
uv run python packages/gateway/scripts/export_platform_contract.py /tmp/heartwood-contract
```

The exporter refuses an existing directory.
Review `contract.json` first: it lists each artifact's path, media type, maturity and SHA-256.
Request schemas use validation mode and response schemas use serialization mode, so their fields match the owning parser or serializer.
JSON Schema validation alone does not perform the cross-field checks of the owning parsers, such as endpoint normalization and duplicate identities.
Distribute the complete directory with the exact source revision or release digest.

## Conformance Checks

### Check Platform Service Responses

Capture a response with an independently authenticated platform client, then check it offline:

```bash
uv run python packages/adapters/scripts/check_platform_response.py model-catalog models-page.json
uv run python packages/adapters/scripts/check_platform_response.py mcp-tools tools-result.json
uv run python packages/adapters/scripts/check_platform_response.py mcp-tasks fixtures/synthetic/platform-contract/mcp-tasks.json
uv run python packages/adapters/scripts/check_platform_response.py planning-overview.schema.json fixtures/synthetic/platform-contract/batch.overview.json
```

The first argument is `model-catalog`, `mcp-tools`, `mcp-tasks` or a draft schema filename.
The checker never contacts a service.
It prints a JSON report with its `checks` and `limitations` and exits with status 0, or prints "Response could not be read or failed contract validation." and exits with status 1.

| Check | Validates | Does not validate |
|---|---|---|
| `model-catalog` | Page structure and unique, nonempty model identifiers | Model metadata, eligibility, protocol behavior, other pages |
| `mcp-tools` | Descriptions, unique names, four explicit boolean annotations, object input and output schema syntax | Schema references, tool calls, other pages, safety claims |
| `mcp-tasks` | Each exchange in a transcript of `server_capabilities` and `request`/`response` pairs against the pinned Tasks definitions | Transition history, input lineage, the complete core protocol |
| Draft schema | Structure and the owning parser's cross-field consistency | Issuer identity, referenced document bytes, current validity |

Annotations and output schemas are optional upstream but required by this profile.
An empty catalog or tool list is valid for an identity with nothing available.
The same checks are available in Python as `validate_model_catalog`, `validate_mcp_tools_list` and `validate_platform_message(schema_filename, response)` in `heartwood.adapters.service_conformance`, and `validate_mcp_task_exchange(request, response, server_capabilities=..., result_schema="platform-estimate.schema.json")` in `heartwood.adapters.mcp_tasks_conformance`.
A passing check never makes a parsed `authorized` field, tool hint or decision record an authorization token.

### Runtime Conformance

Offline checks do not establish runtime controls.
An integration must also demonstrate, in a synthetic environment:

- project settings cannot widen the deployment ceiling;
- approvals bind the complete reviewed action contents and reject changed arguments or groups;
- worker tools cannot reach credentials, launch capabilities, canonical state or management authority;
- inference and platform services remain independent optional capabilities; and
- outside operations are reauthorized by the destination and uncertain outcomes are reconciled before any retry.

For the isolated worker, `deploy/qualify-native-worker.py` runs the native qualification and `deploy/qualification_evidence.py` verifies its evidence; see [Native Qualification Evidence](../architecture/testing.md#native-qualification-evidence).
[Platform API and Access Requirements](platform-requirements.md#contract-bundle-and-acceptance) lists the positive and rejected controls to return for each capability.

## Validate a Platform

Heartwood advertises a platform capability only at the evidence level it has reached, as defined in [Testing and Evidence](../architecture/testing.md#claims):

1. **Implemented:** an adapter reports capabilities, detection and policy, with contract tests.
2. **CI validated:** conformance, ingress, credential-boundary, persistence and interface tests pass in a production-derived image.
3. **Live synthetic validated:** the published artifact completes a synthetic task on the platform, including approval, file verification, replay and audit export.
4. **Institution approved:** separate institutional evidence covers the deployment; it is never inferred from the levels above.

The built-in generic, Terra and Carina adapters are CI validated.
Follow [Validate Conformance](platform-integration.md#validate-conformance) and [Validate the Deployment](index.md#validate-the-deployment), then confirm the [deployment security checklist](security.md#deployment-security-checklist) before a wider rollout.
