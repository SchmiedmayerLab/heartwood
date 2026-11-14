<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Platform API and Access Requirements

Heartwood provides research sessions, reviewed actions and audit records while the research platform retains authority over identity, data, compute, inference and spending.
This guide specifies the APIs, access and deployment controls needed to integrate a platform such as Terra or Seven Bridges.
It is intended for platform engineering, security and research-computing teams.
The [Platform Contract](platform-contract.md) defines message semantics and failure behavior; the generated OpenAPI and schema bundle provide the machine-readable definitions.

## Select the Integration Scope

Hosting, inference and access to research services are independent capabilities.
An inference-only deployment does not require workflow submission, billing administration or a platform tool service.
Start with a synthetic project and the smallest selected access scope.

| Capability | What we request from the platform | Heartwood availability |
|---|---|---|
| Run the application | A supported application artifact, durable storage and an authenticated researcher route | Terminal, browser and notebook availability depend on the platform adapter |
| Isolate complete workers | An outside sandbox control plane, protected canonical state, selected read-only inputs, bounded writable storage and enforced egress | Native reference adapter available; exact deployment qualification required |
| Use approved inference | Authorized model discovery and an approved inference endpoint with streaming and native tool calls | Built-in connections and configured compatible services; deployment connection-manifest startup is unavailable |
| Inspect research metadata | Authorized project, dataset, file and workflow descriptions | Draft integration profile and offline conformance checks; authenticated platform-service runtime unavailable |
| Execute research workflows | Native submission, status, results and cancellation APIs with independent authorization | Draft operation contract; platform execution integration unavailable |
| Estimate work and inspect budgets | Platform-owned estimates, observed spend and allocation constraints | Draft schemas only; no runtime budget integration or Heartwood billing cap |

The service-access requests below specify an integration target, not an assertion that a Terra or Seven Bridges service connector is installed.
The Terra image supports its documented terminal and notebook paths; it does not implement Terra workflow API orchestration.
No built-in Seven Bridges adapter is advertised.
Installation, schema conformance, live validation and institutional approval are separate evidence levels.

## API Directions and Ownership

| Direction | Interface | Authority |
|---|---|---|
| Researcher interface → Heartwood gateway | Generated gateway OpenAPI, server-sent events and WebSocket extension | Heartwood validates ingress, launch capability, commands and approvals |
| Sandboxed worker → inference transport → provider | Existing provider API and model identity | Outside transport controls routing and credentials; provider controls account, model and tenant authorization |
| Trusted service adapter → platform | Existing platform APIs, optionally exposed through the draft MCP integration profile | Platform authenticates each call and enforces project, dataset, compute and billing permissions |
| Trusted launcher → sandbox infrastructure | Existing infrastructure control API | Operator owns admission, policy, runtime identity and observed revocation |
| Trusted gateway → signer and records service | Authenticated checkpoint-signing interface and versioned object-storage API | Independent signer authorizes checkpoint publication; records service enforces scoped access and retention |

The sandbox management API is not an agent tool or a researcher-facing Heartwood API.
OpenShell control objects are specific to the reference adapter; a platform is not required to expose them as portable integration messages.
Reuse supported platform APIs and identity mechanisms rather than adding a second scheduler, provider proxy or secret store.
MCP can describe authorized tools; it does not replace native workflow semantics or create authorization.

## Hosting and Researcher Access

For a workspace deployment, provide one authorized research directory and persistent canonical state.
For managed isolation, bind the state directory outside the research directory through an operator-owned deployment manifest and keep it inaccessible to workers.
State storage must support native advisory locks and preserve the project UUID across restart and restore.
An object-store mount is not sufficient merely because it exposes files.
The `--deployment` manifest is accepted by `heartwood gateway serve`; standalone terminal, notebook and maintenance commands do not automatically consume it.
Qualify the selected interface against the explicitly bound gateway.

Provide the following connection details without secret values:

- exact application artifact digest, supported architecture and execution mode;
- research directory, external state location and operator responsible for their binding;
- authenticated public origin, exact base path, proxy source ranges and TLS termination;
- secret-manager reference or protected descriptor source for the gateway capability;
- gateway lifecycle, session expiry, backup and restoration arrangements; and
- available interfaces and the synthetic environment used to qualify them.

The proxy must authenticate and authorize the researcher, strip client-supplied forwarding and identity headers, and prevent direct access to the gateway.
Use one gateway per researcher and project; Heartwood records one launching principal and does not verify individual users sharing that gateway.
Native OpenID Connect verification and platform token exchange are not implemented by Heartwood.
Use separate authenticated origins for mutually untrusted projects: different URL prefixes on one origin do not isolate browser storage from scripts on that origin.

### Gateway API Contract

The complete route inventory, methods, request and response schemas, status codes and security scheme are generated from the owning contracts in `gateway.openapi.json`.
The [Gateway API reference](../reference/gateway-api.md) explains command bodies, approval targets, retry behavior, error handling and streaming clients.
The following are the principal integration surfaces, relative to the configured gateway base path:

| Surface | Implemented API | Purpose |
|---|---|---|
| Launch | `POST /launch` | Exchange a one-time launch token for a gateway capability |
| Discovery | `GET /project/capabilities`, `GET /project/readiness`, `GET /project/startup` | Discover supported interfaces and the next valid setup step |
| Sessions | `GET /sessions`, `POST /sessions`, `GET /sessions/{session_id}` | List, create and inspect sessions |
| Commands | `POST /sessions/{session_id}/commands` | Submit typed command envelopes; handlers validate command-specific payloads |
| Projection | `GET /sessions/{session_id}/projection` | Obtain the shared interface projection |
| Replay | `GET /sessions/{session_id}/events?after=…` | Retrieve durable events and the projection after a known cursor |
| Streaming | `GET /sessions/{session_id}/events/stream?after=…` | Receive server-sent event updates |
| WebSocket | `/sessions/{session_id}/events` | Receive the same event/projection contract over a WebSocket |
| Audit export | `GET /sessions/{session_id}/audit-export` | Obtain the content-minimized session audit export |

Gateway HTTP API requests require `X-Heartwood-Capability` after ingress validation; `/launch` is the token exchange described by its own OpenAPI security entry.
The operator can supply the capability through `--capability-fd`; environment delivery is rejected.
Browser WebSockets authenticate in their first JSON frame within five seconds, or receive the capability header from the trusted proxy.
Capability cookies are not accepted, and the gateway capability must never reach the worker.

Proxies must support streaming without response buffering and WebSocket upgrades.
Clients reconnect using the `after` query cursor; `Last-Event-ID` replay is not implemented.
New requests during shutdown receive `503`, and WebSockets close with `1012`; reconnect to the replacement gateway using the retained cursor.
Conflicting project maintenance or ownership can produce `409`; it is not permission to force a second writer.
A successful HTTP exchange does not establish that an asynchronous research action completed.
See the [transport and failure contract](platform-contract.md#generate-and-review-the-current-artifacts) for replay, command retry and validation limitations.

## Inference Access

For a compatible inference service, provide its exact API base URL, authenticated model-list endpoint and approved model identifiers.
The model list must be restricted to the caller's authorized models.
The minimum compatible-service profile requires Chat Completions, streaming and native tool calls; declare Responses support separately where available.
Provider-specific routes retain their SDK-supported protocols.

Supply model context and output limits, tool support, rate limits, quota behavior, supported data classes, retention terms and region restrictions where known.
Missing declarations remain unknown; a model name or API-compatible response does not establish data eligibility.
Heartwood's draft model-metadata schema supplements provider facts but is not consumed as a runtime model catalog.

For isolated workers, the approved external inference transport holds the real credential and provides only a placeholder to the worker.
The reference adapter reuses OpenShell inference transport; an institutional gateway can remain the provider endpoint behind it.
Keep credentials out of project files, worker mounts, worker environments, command arguments and logs.
Provision through the platform's secret manager or protected operator mechanism, with documented rotation and revocation.

Enforce model and tenant restrictions at the destination or an existing authorized inference gateway.
A network allowlist for a hostname does not constrain which account, model, project or request body a compromised worker uses there.
Specify whether the credential authorizes one model or an explicitly approved model set and test rejected alternatives accordingly.
Demonstrate revocation independently of key rotation, including existing connections and in-flight calls.

Heartwood-managed local inference uses an owner-only Unix socket and process supervision, without a TCP fallback.
It is a local launcher facility, not a worker-accessible platform service.
Using local model compute for an isolated worker requires a separately authenticated outside route; the reference deployment does not automatically bridge the local socket into its inference transport.

## Research Service APIs and Permissions

Select these capabilities individually; inspection does not imply permission to execute, export data or change membership.
Credentials belong to the trusted outside adapter, with the platform checking the caller's effective permission on every request.
Do not give raw platform credentials or unrestricted API tools to the sandboxed worker.

| Capability | Required API behavior | Minimum access to request |
|---|---|---|
| Project discovery | Resolve an authorized project and its stable identifier | Read selected project metadata; no organization-wide enumeration requirement |
| Dataset and file discovery | Return identifiers, metadata, versions or checksums, and access restrictions | Read metadata for selected resources |
| Input access | Resolve authorized content or scoped retrieval references | Read only the selected data; metadata permission alone is insufficient |
| Workflow discovery | Retrieve immutable workflow/app revision, input schema and required resources | Read approved workflow definitions |
| Submission | Submit the exact reviewed native request and return an operation identifier | Execute only in the selected project and authorized billing context |
| Monitoring | Read authoritative status, outputs and permitted logs by operation identifier | Observe the authorized operation; log access remains data access |
| Cancellation | Request cancellation and subsequently observe the terminal outcome | Cancel the authorized operation; no blanket project deletion permission |
| Output publication | Write reviewed outputs to an authorized destination | Explicit destination-scoped write or export permission |
| Budget observation | Return timestamped allocation and spending evidence, including unknown or delayed values | Read the relevant project allocation; no billing-administrator role by default |

For submission, retain the exact workflow revision, input references, effective limits, project and platform-owned billing binding in the reviewed proposal.
Ask the platform which APIs provide idempotency keys or reliable lookup after a lost response; do not assume that a native POST is safe to retry.
If acceptance is uncertain, reconcile using the native operation identity before any resubmission.
Cancellation acknowledgment does not prove the operation stopped or that charges ceased.
Data retrieval, logging and export can incur costs even when the API permission is read-only.

An MCP service for these capabilities must describe its tools, input/output schemas, permission requirements and explicit `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` annotations.
These annotations are untrusted hints, not grants of authority.
The [draft service and operation contract](platform-contract.md#platform-services) specifies review binding, asynchronous results and failure states.
It does not require a platform to replace its existing native APIs or imply a working Heartwood MCP transport.

### Terra

Confirm the Terra deployment and cloud first; the workspace and workflow requirements here apply to the Google Cloud workspace integration.
Use the platform's [API reference](https://api.firecloud.org/) to select the supported workspace, data-table, method-configuration and submission operations.
Terra workflows use WDL and Cromwell; provide the approved workflow revision and its configured inputs rather than a new execution format. See [Terra workflow execution](https://support.terra.bio/hc/en-us/articles/360036379771-Overview-Running-workflows-in-Terra).

Request a synthetic workspace identifier, authorized principal and a declared permission set.
Terra separates Reader, Writer and Owner roles from the ability to compute: request Reader for inspection, and Writer with Can-compute for workflow or interactive-compute launch.
Writer also permits changes to workspace tables and workflow configurations; it is broader than submission-only access, so use a dedicated integration workspace when that additional authority is unacceptable in a shared workspace.
Owner and sharing permissions are not baseline integration requirements.
Workspace charges use its Terra billing project; even some read/copy operations can incur transfer costs. See [Terra workspace access controls](https://support.terra.bio/hc/en-us/articles/360025851892-Sharing-data-and-tools-with-workspace-access-controls).

The public [Terra API specification](https://api.firecloud.org/) defines these workspace routes, relative to `/api/workspaces/{workspaceNamespace}/{workspaceName}`:

| Operation | Native method and relative path | Requested access |
|---|---|---|
| Inspect data-table types | `GET /entities` | Selected workspace metadata |
| Inspect workflow configurations | `GET /methodconfigs` | Approved configuration metadata |
| Inspect submission history | `GET /submissions` | Authorized workflow history |
| Submit the reviewed configuration | `POST /submissions` | Writer with Can-compute in the bound workspace |
| Monitor the submission | `GET /submissions/{submissionId}` | Authorized submission state |
| Read workflow execution metadata | `GET /submissions/{submissionId}/workflows/{workflowId}` | Authorized metadata; outputs may need separate storage access |
| Request abort | `DELETE /submissions/{submissionId}` | Explicit abort permission; observe terminal status afterward |

Confirm these operations and their exact permissions for the selected deployment, plus data-table changes if selected.
Use the native specification for request bodies and version-specific validation; this list does not grant access or promise safe retries.
Confirm bucket/object access and controlled-data entitlements independently of workspace metadata access.
Terra Data Repository has a separate [API and authentication surface](https://support.terra.bio/hc/en-us/articles/4407244155931-How-to-authenticate-troubleshoot-Swagger-for-TDR); include it only when the selected datasets require it.
Use an institution-approved user or service identity with the least privileges needed, following [Terra's service-account guidance](https://support.terra.bio/hc/en-us/articles/360031023592-Best-practices-for-using-service-accounts-in-Terra).
Do not request unrestricted Google Cloud credentials or mount a researcher's cloud credential directory into a worker.

### Seven Bridges

Confirm the specific deployment and regional API base before configuring access.
The public Platform documents AWS US `https://api.sbgenomics.com/v2` and AWS EU `https://eu-api.sbgenomics.com/v2`; other hosted deployments require their own confirmed endpoint.
Its API authenticates with `X-SBG-Auth-Token`; keep the token in the outside service adapter. See [Seven Bridges API overview](https://docs.sevenbridges.com/docs/the-api).

Request an existing synthetic project, its project identifier and approved app revision.
Use native project, file and app APIs for discovery; file identifiers select inputs.
For selected execution access, the native task lifecycle is:

| Operation | Native API, relative to the selected `/v2` base | Reference |
|---|---|---|
| Create a draft task | `POST /tasks` | [Create a draft task](https://docs.sevenbridges.com/reference/create-a-new-task) |
| Start the reviewed task | `POST /tasks/{task_id}/actions/run` | [API task workflow](https://docs.sevenbridges.com/docs/api-quickstart) |
| Read status and outputs | `GET /tasks/{task_id}` | [Get task details](https://docs.sevenbridges.com/reference/get-details-of-a-task) |
| Request cancellation | `POST /tasks/{task_id}/actions/abort` | [Abort a task](https://docs.sevenbridges.com/reference/abort-a-task) |

Request Read for names, metadata and workflows; Copy is a separate permission for content access and downloads.
Execute permits running workflows and aborting tasks; Write permits changes to files and workflows.
Request only the selected capabilities and confirm endpoint-specific prerequisites with the operator.
Admin, membership management and funding-source changes are not baseline requirements. See [Seven Bridges project permissions](https://docs.sevenbridges.com/docs/projects-and-members).
These permissions apply at project scope; they are not per-file or per-task capabilities.
Use a dedicated project and restricted outside adapter when the required authority must be narrower than the platform role.
The platform retains the project billing binding; the worker must not select or modify it.
Do not treat a file download URL or copied file as approval to export controlled data.

## Audit and Records Access

The platform's records service is a separate authority from research storage and workflow execution.
Provide the signer endpoint, its authentication mechanism, approved deployment identifier, public verification keys and key-version identifiers, and the applicable retention policy.
The implemented signer interface is `POST /v1/checkpoints/sign`, accepting `heartwood.audit-checkpoint-sign-request.v1` and returning `heartwood.audit-checkpoint-signature.v1` over authenticated HTTPS.
It is an outside service interface, not a route on the Heartwood gateway.
See [Audit Checkpoints and Retention](audit-checkpoints.md) for exact registry fields, algorithms and verification behavior.

For independently retained evidence, identify the records destination, supported versioning and immutable-retention controls, authorized publishers and readers, and restoration procedure.
Request these permissions separately:

| Principal | Required authority | Boundary |
|---|---|---|
| Trusted gateway | Stage candidate audit and experiment records, read retained history, and request signing for its bound scope | No checkpoint-ordinal publication, signing key, key administration or records-policy administration |
| Signing service | Independently authorize the fixed scope, read candidates and complete retained history, sign and conditionally publish checkpoints | Private key stays in the service or its KMS/HSM; worker cannot invoke it; no retention bypass or deletion authority |
| Independent verifier | Read retained versions, signatures and retention metadata | No write, signing or key-administration permission |
| Worker | No signing or records-service authority | Neither credentials nor network access to these services |

The operator must confirm the platform-specific API actions that implement these roles; this is an access requirement, not a portable IAM policy or an automatically provisioned integration.
For the implemented S3-compatible adapter, provide version listing, exact-version reads and retention metadata, with conditional writes limited to each identity's prefixes.
The bucket must enforce versioning, COMPLIANCE retention and create-only publication independently of the client.
The [records permissions contract](audit-checkpoints.md#records-permissions-and-retention) maps these operations to S3 permissions and identifies the required policy checks.
An arbitrary signing endpoint that returns a valid signature is insufficient for managed evidence: the independently authorized service must retain the checkpoint before returning it.
Retain the association between Heartwood project/session identifiers, platform operation identifiers, key versions and the independently stored records.
Define how loss of signing, retention or external telemetry is detected and which work must stop; a local record or signed retention date alone does not prove outside collection or enforcement.

## Worker and Sensitive-Data Controls

The entire worker, including the model client, tools and subprocesses, belongs inside the externally enforced boundary.
The trusted gateway retains policy, approvals and canonical records outside it.
Task-relevant instructions and verified Skills are projected as content; signed authority records, signing keys, provider credentials and control-plane access are not task context.
A signature on content proves only what its verification establishes; it does not authorize an action by the worker.

Request mandatory filesystem and process controls, resource limits and default-deny network enforcement with exact approved routes.
Qualification must test private and metadata addresses, alternate routes, redirects, peer workers, management endpoints and process inheritance as applicable to the selected infrastructure.
The operator must be able to observe and revoke a worker independently of its messages and retain ownership while an outcome is uncertain.
No Docker socket, infrastructure credential, broad research mount or canonical-state mount belongs inside the worker.
The native reference adapter needs an operator-controlled Docker service; it does not require handing Docker administration to the researcher or agent.

Sensitive-data use additionally requires institution-approved data eligibility, provider agreements, residency, retention, monitoring and output-release controls for the exact deployment.
Heartwood provides selected-input staging and native read-only mounts, but not a complete quarantine or steward-approved release service.
Signed checkpoint export does not establish immutable retention or continuity between checkpoints.
Automatic externally anchored evidence uses the deployment manifest's `evidence` table with complete worker isolation, an approved signer registry and a separately authorized records identity.
Provide the exact records service origin, scoped permissions, retention policy and credential source described in [managed evidence configuration](audit-checkpoints.md#configure-managed-evidence).
Managed checkpoint creation requires a trusted integration with the bound gateway methods; there is no checkpoint-creation HTTP route, and the standalone audit CLI does not consume the deployment manifest.
Required evidence mode blocks the reviewed action while signing or retained-history verification is unavailable.
Buffered mode allows ordinary work during an eligible service outage with a visible evidence gap; outside operations and outside-result disclosure still require retained evidence.
These limitations prevent treating a successful synthetic sandbox run as sensitive-data approval.

## Information to Return

A platform team can return the following non-secret configuration and supporting documentation:

| Item | Requested information |
|---|---|
| Scope | Hosting, inference, metadata, workflow execution, output publication and budget capabilities selected independently |
| Environment | Deployment, cloud, region, synthetic project identifier and technical contact |
| Identity | Approved principal type, audience/scopes, project permissions, token lifetime, secret reference and revocation mechanism |
| APIs | Base URLs, API/specification versions, selected operations, request limits, pagination, retry and error semantics |
| Inference | Exact model identifiers and protocols, permitted model set, data eligibility, quota and retention terms |
| Storage | Research/state locations, project identity binding, locking, backup/restore and input/output controls |
| Network | Proxy origin/base path, gateway access restrictions, approved egress destinations and outside enforcement |
| Compute | Supported worker architecture, isolation mechanism, control-plane owner, image policy and resource limits |
| Operations | Workflow/app revisions, billing binding, status/cancellation behavior and lost-response reconciliation |
| Records | Signer endpoint and identity, verification keys, retention destination and policy, publisher/verifier permissions and restore procedure |
| Evidence | Synthetic test procedure, audit correlation, revocation checks, deployment limitations and institutional approval owner |

Send identifiers and secret-manager references through the integration documentation; deliver credentials through the approved secret provisioning channel.
Do not include real research data or credentials in API examples, tickets or qualification artifacts.

## Contract Bundle and Acceptance

Export the complete machine-readable bundle using the [canonical exporter](platform-contract.md#generate-and-review-the-current-artifacts).
Distribute `contract.json`, `gateway.openapi.json`, all referenced schemas and the upstream provenance/license files together.
Include this requirements guide, the [Gateway API reference](../reference/gateway-api.md) and the [Platform Contract](platform-contract.md), along with the exact application source revision or released artifact digest.
The material is suitable for integration and access review; it is not an assertion that the described draft services are deployed.
The index identifies artifact hashes and whether each document describes a current contract, a draft message or an unchanged upstream specification.
The operator deployment manifest and native worker profile remain implementation-specific inputs documented in the [CLI reference](../reference/cli.md#operator-commands); they are not exported portable platform schemas.

Validate captured model and service responses with the existing offline conformance helpers before testing authenticated transport.
Then qualify the exact artifact in the synthetic environment: ingress rejection, permission denial, action-content binding, worker admission/revocation, inference authorization, persistence and restore, and safe handling of lost responses.
Require both successful authorized operations and rejected unauthorized controls; a failed request alone can indicate a broken service rather than effective policy.
Record the available capabilities and unresolved limitations against that deployment, following [Validate a Platform](platform-contract.md#validate-a-platform).

For each selected capability, return both an authorized positive control and the corresponding rejected alternative:

| Capability | Authorized control | Rejected or interrupted control |
|---|---|---|
| Gateway access | Owning researcher reaches the declared route and reconnects to the event stream | Another principal, invalid capability, wrong origin or direct upstream connection cannot enter |
| Inference | Approved model and identity complete a bounded synthetic tool call | Disallowed model or tenant, direct credential access and revoked worker route fail |
| Data access | Selected synthetic input is readable through its approved staging route | Unselected resources, writes to read-only inputs and unauthorized export fail |
| Workflow execution | Exact reviewed request yields a correlated native operation | Changed inputs or project, expired approval and replay with conflicting content fail; lost replies reconcile without resubmission |
| Retained evidence | Independently authorized checkpoint is readable and verifies after restore | Wrong-scope signer requests, unauthorized publication, replacement or deletion fail; required-mode outage prevents dispatch |
| Worker lifecycle | Outside controller admits and subsequently revokes the exact attempt | Worker cannot reach protected state or management authority; uncertain deletion does not release ownership |

For capabilities without a Heartwood runtime integration, platform-side evidence establishes the native service behavior only.
It does not establish end-to-end Heartwood support.
