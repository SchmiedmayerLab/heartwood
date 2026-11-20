<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Platform API and Access Requirements

These are the APIs, access and deployment controls a platform team such as Terra or Seven Bridges provides to integrate Heartwood.
The [Platform Contract](platform-contract.md) defines the message semantics, versioning and conformance checks behind each request.

## Select the Integration Scope

Hosting, isolated workers, inference and research services are independent capabilities; select only what the integration needs and start with a synthetic project.

| Capability | Requested from the platform | Heartwood status |
|---|---|---|
| Run the application | A release artifact, durable storage and an authenticated researcher route | Current; interfaces depend on the platform adapter |
| Isolate complete workers | A Docker engine or a Kubernetes namespace, and a sandbox gateway under operator control, protected state, selected read-only inputs, bounded writable storage and enforced egress | Current for the OpenShell Docker reference adapter and for the OpenShell Kubernetes driver under the [Kubernetes worker path](#kubernetes-worker-path) requirements; qualified only against the [sandbox gateway build](#sandbox-gateway), the Kubernetes driver on a kind cluster |
| Use approved inference | Authorized model discovery and an inference endpoint with streaming and native tool calls | Current through built-in and configured routes; connection-manifest startup unavailable |
| Inspect research metadata | Authorized project, dataset, file and workflow descriptions | Draft profile and offline checks; no built-in connector |
| Execute research workflows | Native submission, status, results and cancellation with independent authorization | Draft profile; [platform job operations](platform-contract.md#platform-job-operations) through an MCP job service, otherwise the trusted Python [outside operation](platform-contract.md#outside-operation-admission) interface with a platform-specific adapter |
| Estimate work and inspect budgets | Platform-owned estimates, observed spend and allocation constraints | Draft schemas only |

The service requests below describe an integration target; no Terra workflow or Seven Bridges connector is installed.
The Terra image supports the documented terminal and notebook paths.

## Hosting and Researcher Access

Provide one authorized research directory and persistent canonical state on storage with native advisory locks that preserves the project UUID across restart and restore; an object-store mount is not sufficient because it exposes files.
For a managed project, bind state outside the research directory with an operator-owned [deployment manifest](../reference/configuration.md#deployment-manifest) and keep it inaccessible to workers.
The manifest is read only by `heartwood gateway serve --deployment`, `heartwood project migrate` and `heartwood project recover-migration`; qualify each interface against the gateway it binds.

Provide, without secret values:

- the artifact digest, architecture and execution shape;
- the research directory, state location and the operator responsible for their binding;
- the authenticated public origin, exact base path, proxy source ranges and TLS termination;
- the secret-manager reference or protected descriptor source for the gateway capability;
- gateway lifecycle, session expiry, backup and restoration arrangements; and
- the available interfaces and the synthetic environment used to qualify them.

The proxy authenticates and authorizes the researcher, strips client-supplied forwarding and identity headers, supports unbuffered streaming and WebSocket upgrades, and prevents direct access to the gateway.
Use one gateway per researcher and project, and separate authenticated origins for mutually untrusted projects.
With [`identity-proxy` ingress](security.md#researcher-identity) the gateway verifies one researcher's OpenID Connect identity itself, and the proxy only forwards requests.
For a platform service whose [destination](../reference/configuration.md#mcp-destinations) or [`[planning]` table](../reference/configuration.md#planning-service) declares a `token_exchange`, the gateway exchanges the researcher's access token at the identity provider for a token with that service's audience, as [Service Tokens](platform-contract.md#service-tokens) specifies.
The [Gateway API reference](../reference/gateway-api.md) and the generated `gateway.openapi.json` define every route.

## Inference Access

For a compatible inference service, provide the API base URL, the authenticated model-list endpoint and the approved model identifiers.
The model list returns only the caller's authorized models.
The minimum profile is Chat Completions with streaming and native tool calls; declare Responses support separately.
State context and output limits, tool support, rate limits, quota behavior, permitted data classes, retention terms and region restrictions where known; missing declarations stay unknown.
Declare each connection's [data handling](platform-contract.md#route-data-handling): the destination class of the complete route, including any gateway and its ultimate providers, and the authority that governs retention.
A deployment that must keep content within a boundary lists the permitted destinations in its policy's `allowed_data_destinations`; a profile without external egress lists only `same-compute`.

For isolated workers, the outside inference transport holds the real credential and gives the worker only a placeholder.
The reference adapter uses the OpenShell inference transport; an institutional gateway can remain the provider endpoint behind it.
Enforce model and tenant restrictions at the provider or an authorized inference gateway, because a hostname allowlist does not constrain which account, model or request a compromised worker sends there.
State whether the credential authorizes one model or an approved set, and demonstrate revocation separately from rotation, including existing connections.
A Heartwood-managed local model server is not reachable from an isolated worker; local compute for a worker needs a separately authenticated outside route.

## Research Service APIs and Permissions

Select each capability individually; inspection does not imply permission to execute, export or change membership.
Credentials belong to the trusted outside adapter, and the platform checks the caller's permission on every request.
Never give raw platform credentials or unrestricted API tools to a worker.

| Capability | Required API behavior | Minimum access to request |
|---|---|---|
| Project discovery | Resolve an authorized project and its stable identifier | Read selected project metadata |
| Dataset and file discovery | Return identifiers, metadata, versions or checksums, and access restrictions | Read metadata for selected resources |
| Input access | Resolve authorized content or scoped retrieval references | Read only the selected data |
| Workflow discovery | Retrieve the immutable workflow or app revision, input schema and resources | Read approved workflow definitions |
| Submission | Submit the exact reviewed native request and return an operation identifier | Execute only in the selected project and billing context |
| Monitoring | Read status, outputs and permitted logs by operation identifier | Observe the authorized operation |
| Cancellation | Request cancellation and observe the terminal outcome | Cancel the authorized operation only |
| Output publication | Write reviewed outputs to an authorized destination | Destination-scoped write or export permission |
| Budget observation | Return timestamped allocation and spending, including unknown or delayed values | Read the relevant allocation; no billing-administrator role |

State which APIs accept idempotency keys or support lookup after a lost response; a native POST is not assumed safe to retry.
Cancellation acknowledgement does not prove that work or charges stopped, and data retrieval, logging and export can incur cost under read-only permissions.
An MCP service for these capabilities follows the [service discovery profile](platform-contract.md#service-discovery).

### Terra

These requirements apply to the Google Cloud Terra workspace integration; confirm the deployment and cloud first.
Terra workflows use WDL and Cromwell; provide the approved workflow revision and configured inputs. See [Terra workflow execution](https://support.terra.bio/hc/en-us/articles/360036379771-Overview-Running-workflows-in-Terra).

Request a synthetic workspace, an authorized principal and a declared permission set.
Request Reader for inspection, and Writer with Can-compute for workflow or interactive-compute launch.
Writer also permits changes to workspace tables and method configurations; use a dedicated integration workspace when that authority is unacceptable.
Owner and sharing permissions are not required.
Workspace charges use its Terra billing project, and some read or copy operations incur transfer costs. See [Terra workspace access controls](https://support.terra.bio/hc/en-us/articles/360025851892-Sharing-data-and-tools-with-workspace-access-controls).

The [Terra API specification](https://api.firecloud.org/) defines these routes relative to `/api/workspaces/{workspaceNamespace}/{workspaceName}`:

| Operation | Native method and path | Requested access |
|---|---|---|
| Inspect data-table types | `GET /entities` | Selected workspace metadata |
| Inspect workflow configurations | `GET /methodconfigs` | Approved configuration metadata |
| Inspect submission history | `GET /submissions` | Authorized workflow history |
| Submit the reviewed configuration | `POST /submissions` | Writer with Can-compute |
| Monitor the submission | `GET /submissions/{submissionId}` | Authorized submission state |
| Read workflow execution metadata | `GET /submissions/{submissionId}/workflows/{workflowId}` | Authorized metadata; outputs may need storage access |
| Request abort | `DELETE /submissions/{submissionId}` | Abort permission; observe terminal status afterward |

Use the native specification for request bodies and validation.
Confirm bucket and object access and controlled-data entitlements separately from workspace metadata access.
Include the Terra Data Repository [API](https://support.terra.bio/hc/en-us/articles/4407244155931-How-to-authenticate-troubleshoot-Swagger-for-TDR) only when the selected datasets require it.
Use a least-privilege user or service identity following [Terra's service-account guidance](https://support.terra.bio/hc/en-us/articles/360031023592-Best-practices-for-using-service-accounts-in-Terra), and never mount a researcher's cloud credential directory into a worker.

### Seven Bridges

Confirm the deployment and regional API base: the public Platform documents `https://api.sbgenomics.com/v2` (AWS US) and `https://eu-api.sbgenomics.com/v2` (AWS EU).
The API authenticates with `X-SBG-Auth-Token`; keep the token in the outside adapter. See the [Seven Bridges API overview](https://docs.sevenbridges.com/docs/the-api).

Request a synthetic project, its identifier and an approved app revision.
Use the native project, file and app APIs for discovery; file identifiers select inputs.
The task lifecycle is:

| Operation | Native API relative to `/v2` | Reference |
|---|---|---|
| Create a draft task | `POST /tasks` | [Create a draft task](https://docs.sevenbridges.com/reference/create-a-new-task) |
| Start the reviewed task | `POST /tasks/{task_id}/actions/run` | [API task workflow](https://docs.sevenbridges.com/docs/api-quickstart) |
| Read status and outputs | `GET /tasks/{task_id}` | [Get task details](https://docs.sevenbridges.com/reference/get-details-of-a-task) |
| Request cancellation | `POST /tasks/{task_id}/actions/abort` | [Abort a task](https://docs.sevenbridges.com/reference/abort-a-task) |

Read covers names, metadata and workflows; Copy covers content access and downloads; Execute covers running and aborting tasks; Write covers changes to files and workflows.
Admin, membership and funding-source permissions are not required. See [Seven Bridges project permissions](https://docs.sevenbridges.com/docs/projects-and-members).
These permissions apply per project, so use a dedicated project when the required authority must be narrower than a platform role.
The platform keeps the project billing binding, and a download URL or copied file is not approval to export controlled data.

## Audit and Records Access

Provide the signer endpoint, its authentication mechanism, the approved deployment identifier, public verification keys with key versions, and the retention policy.
The signer implements `POST /v1/checkpoints/sign` as described in [Audit and Records](platform-contract.md#audit-and-records); it is an outside service, not a gateway route.

For managed evidence, also provide the records service origin, versioning and Object Lock retention, authorized publishers and readers, and the restore procedure, with these principals:

| Principal | Authority | Boundary |
|---|---|---|
| Gateway | Stage candidate audit and experiment records, read retained history, request signing for its scope | No checkpoint publication, signing key or policy administration |
| Signing service | Authorize the fixed scope, read candidates and retained history, sign and conditionally publish checkpoints | Private key stays in the service or its KMS or HSM; no retention bypass or deletion |
| Independent verifier | Read retained versions, signatures and retention metadata | No write, signing or key administration |
| Worker | None | No credentials for or network route to these services |

[Records Permissions and Retention](audit-checkpoints.md#records-permissions-and-retention) maps these roles to S3 actions.
A signing endpoint that returns a valid signature without retaining the checkpoint first does not satisfy managed evidence.
Define how loss of signing, retention or telemetry is detected and which work stops.

## Worker and Sensitive-Data Controls

The complete worker, including its model client, tools and subprocesses, belongs inside the externally enforced boundary; the gateway keeps policy, approvals and canonical records outside it.
Request mandatory filesystem and process controls, resource limits and default-deny networking with exact approved routes.
Qualification must cover private and metadata addresses, alternate routes, redirects, peer workers, management endpoints and process inheritance on the selected infrastructure.
No Docker socket, infrastructure credential, research-directory mount or state mount belongs in the worker.
The reference adapter needs an operator-controlled Docker engine; it does not hand Docker administration to the researcher or agent.

### Kubernetes Worker Path

With `driver = "openshell-kubernetes"`, the worker of a session runs as pods that the OpenShell Kubernetes driver creates in one namespace.
Heartwood reads those pods, their NetworkPolicies and the namespace through the Kubernetes API before it admits a worker; the [accepted values](../architecture/system.md#kubernetes-worker-admission) are listed with the Docker values.
The worker's `/workspace` on this driver lives on the container's writable layer; the workload pod never restarts its container, so the directory lasts as long as the worker ([Managed Native Worker](../architecture/system.md#managed-native-worker)).
The platform provides what follows.
The reference platform chart in `reference-platform/chart` provides each namespace and gateway item for a session, and the [Kubernetes driver qualification](../architecture/testing.md#kubernetes-driver-qualification) runs against it on a kind cluster.

The cluster and its nodes:

| Requirement | Reason |
|---|---|
| A network plugin that enforces ingress and egress NetworkPolicy | The worker's network confinement is the driver's NetworkPolicies. The Kubernetes API does not report enforcement, so run a blocked-connection control with a reachable control on the cluster. |
| The Agent Sandbox controller with the `sandboxes.agents.x-k8s.io` resource | The driver creates each sandbox as a Sandbox resource. |
| A kubelet process limit for each pod (`podPidsLimit`) | A pod does not show a process limit, so admission cannot read one. |
| Nodes with cgroup v2 | The container runtime then gives each container a private cgroup namespace, which a pod does not show. |
| The container runtime's default AppArmor profile or SELinux policy on every node | The pods carry no profile field; the `restricted` Pod Security level refuses an unconfined profile. |
| A registry from which the nodes pull the worker image and the OpenShell supervisor and sandbox runtime images by digest | The manifest names the worker image as `image_repository` and `image_digest`, and admission compares the digest each container runs with the manifest. |
| A container runtime that supports `supplementalGroupsPolicy: Strict` | The staging pod sets it, so the groups of its user in the worker image are not added; a runtime without the feature does not start the pod. |
| A StorageClass `heartwood-attempt` that provisions `ReadWriteOnce` volumes and deletes them with their claim | Inputs and Skills are staged into two claims of 256 MiB for each attempt. Admission reads the claims, and a claim does not show whether its volume is a host path. |

The namespace of a session:

| Requirement | Reason |
|---|---|
| One namespace and one sandbox gateway for each researcher and project | Every client of one sandbox gateway has the same identity there. |
| The label `pod-security.kubernetes.io/enforce` with the value `restricted`, and no pinned version other than `latest` | Admission refuses any other value. |
| A NetworkPolicy that selects every pod and denies ingress and egress | Staging requires that its pod has no network access, and the worker's pods receive only the driver's own allowances. |
| No further NetworkPolicy that selects workload pods and has a rule | Admission refuses a worker whose pods another policy allows traffic. |
| Ingress to the sandbox gateway on port 8080 from supervisor pods and from the session's Heartwood gateway, and egress from the sandbox gateway to cluster DNS and the Kubernetes API | The driver's supervisors and Heartwood connect to the sandbox gateway; exec and port forwarding into a worker travel over the supervisor's connection. |
| A ServiceAccount `hw-staging` that mounts no token and holds no role | The staging pod runs under it as user and group `10002`. |
| When the namespace has a ResourceQuota, a LimitRange with default requests and limits | The driver's supervisor and init containers declare none. |
| No principal with `pods/ephemeralcontainers`, `pods/attach` or `pods/portforward` in the namespace | A container added to the workload pod after admission runs outside the sandbox process, and attach and port forwarding reach the workload container without passing the supervisor. |

The credential of the Heartwood gateway holds the permissions listed for the [`kubeconfig` key](../reference/configuration.md#kubernetes-driver) in that namespace and nothing else.
An admission policy limits what it creates there to pods of the staging shape, which carry only the labels `heartwood.project`, `heartwood.attempt` and `heartwood.role`, and claims named `hw-attempt-*` with the StorageClass `heartwood-attempt`, and admits exec sessions only into pods named `hw-staging-*`.
The staging pod runs the worker image, so the policy admits each worker image the deployment manifests name, the current and the next one during a change of the image.
The reference platform ships this as the ValidatingAdmissionPolicy `heartwood-staging-shape`.
Because such exec sessions are admitted by pod name, no other ServiceAccount may create a pod or a Sandbox resource whose name begins with `hw-staging` in the namespace; the reference platform ships this as the ValidatingAdmissionPolicy `heartwood-staging-names`.
The policy applies only to requests of ServiceAccounts, so the cluster's controllers must create pods with their own ServiceAccount credentials, as `kube-controller-manager --use-service-account-credentials=true` does.

The [sandbox gateway](#sandbox-gateway), with these settings:

| Setting of the OpenShell chart | Value | Reason |
|---|---|---|
| `server.drivers.kubernetes.allowDriverConfig` | `true` | Heartwood passes the read-only mounts of `/inputs` and `/skills` as driver configuration. |
| `allowed_pod_labels` under `openshell.drivers.kubernetes` in `gatewayConfig` | `heartwood.project`, `heartwood.attempt` and `heartwood.role` | The driver copies only listed labels to the pods, and Heartwood finds the pods of an attempt by them. |
| `server.sandboxUid` and `server.sandboxGid` | `10001` | Admission requires the workload to run as this user and group. |
| `server.drivers.kubernetes.workspaceMode` | `shared` | Sandboxes run in the gateway's own namespace, which is the namespace the manifest names. |
| `supervisor.image.digest` and `sandboxRuntime.image.digest` | The digests in the manifest keys `supervisor_image_digest` and `sandbox_image_digest` | Admission compares the images the pods run. |
| `replicaCount` | `1` | The driver serializes the operations on one sandbox inside one gateway process, and the deletion behavior it reports was examined and qualified with one replica. |
| `mutation_admission_limit_per_caller` under `openshell.gateway` in `gatewayConfig` | `100000` or more | Heartwood sends each creation with a request identifier, and a creation without a recorded outcome keeps its admission slot for the life of the gateway's store. |
| `supervisor.network.additionalCaConfigMapName` | A ConfigMap with the certificate authority of the model route, when that authority is not publicly trusted | The supervisor makes the TLS connection to each destination the sandbox policy allows. |

Each sandbox has its own supervisor pod; the shared supervisor topology is not qualified.
The driver's policy `openshell-sandbox-supervisors` allows supervisor pods all egress, so the namespaces of other services deny ingress by default and admit supervisor pods only where a route is intended.

### Sandbox Gateway

Heartwood provides a sandbox gateway build: the upstream OpenShell gateway at the revision recorded in `images/worker/candidate/gateway.json`, with the patches listed there, both the Docker and the Kubernetes compute drivers, and version `0.1.9+heartwood.3`, a local version that no upstream release uses.
Native qualification runs against this build on both drivers, and revocation and cleanup claims apply to this build only.
The patches, their purpose and the upstream change that allows their removal are listed in [Testing and Evidence](../architecture/testing.md#candidate-builds).

#### Obtain and Verify

Build the gateway from the release's commit with the shared build script, as described in [Testing and Evidence](../architecture/testing.md#candidate-builds):

```bash
uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py \
  --gateway --gateway-tests --output gateway-build --cache gateway-cache
```

The script verifies every source, patch and image input against its recorded digest, runs the gateway's deletion tests and the Kubernetes driver's tests, and writes `gateway-build-record.json`.
The record holds the purpose `deployable`, the version, the upstream source revision, the enabled driver features, the digests of the manifest and the build script, the binary digests and the image identifier.
The image carries the labels `org.opencontainers.image.title=heartwood-sandbox-gateway`, `org.opencontainers.image.version` and `org.opencontainers.image.revision` with the same version and source revision.

The build establishes provenance, and a rebuild can produce other binary digests.
The `gateway_build` entry of each [qualification evidence](../architecture/testing.md#native-qualification-evidence) records the exact build that the run qualified: the Docker run's evidence for `openshell-docker` and the [Kubernetes qualification](../architecture/testing.md#kubernetes-driver-qualification) evidence for `openshell-kubernetes`.
That evidence covers a gateway whose version, source revision, manifest digest, features and binary digests equal the entry, packaged with OpenShell's `deploy/docker/Dockerfile.gateway` at that source revision, whose digest the entry records as `dockerfile_sha256`, on the base image `images.gateway_base` of `gateway.json`.
A deployment whose gateway differs from that runs native qualification against its own build and retains the evidence: the Docker qualification for `openshell-docker` and the Kubernetes qualification for `openshell-kubernetes`.

To compare a running gateway with the evidence, copy `/usr/local/bin/openshell-gateway` out of the image it runs, for example with `docker create` and `docker cp`, because the image has no shell, and compare its SHA-256 with the `openshell-gateway` entry of the `gateway_build` entry's `binaries`.
Compare the leading entries of the image's layers, `docker image inspect --format '{{json .RootFS.Layers}}' IMAGE`, with the layers of `images.gateway_base` for the entry's `architecture`, `docker buildx imagetools inspect BASE --format '{{json (index .Image "linux/ARCH").RootFS.DiffIDs}}'`, and the version the running gateway reports with the entry's `version`.
Each worker attempt's evidence records the version, driver and capabilities the running gateway reported.

#### Register

Register the gateway's endpoint and mutual TLS identity with `openshell gateway add` under the manifest's `native_config_home`, and name it in the deployment manifest's [`gateway` key](../reference/configuration.md#managed-worker).
In the gateway's own configuration file, set exactly one compute driver under `[openshell.gateway]`: `compute_driver = "docker"` for `openshell-docker` or `compute_driver = "kubernetes"` for `openshell-kubernetes`, because a gateway with both drivers and no selection chooses one by detection.
The reference platform deploys the image per session namespace from its `images.openshellGateway` value, by digest.

Before it serves, launches a worker or reconciles a retained attempt, Heartwood reads the registered gateway's info through the `GetGatewayInfo` API.
It requires exactly one compute driver whose negotiated capabilities include the three capabilities listed under [Behavior](#behavior), and otherwise stops with `HW-WORKER-001` before any sandbox, volume or deletion request.
The driver must be the implementation the manifest's `driver` selects: `openshell/docker` for `openshell-docker` and `openshell/kubernetes` for `openshell-kubernetes`.
A gateway that reports the other implementation, or both, is refused.
The gateway version does not satisfy the check.
The decision is retained, with the reported version, driver and capabilities, in each worker attempt's evidence: an accepted gateway in the runtime binding and revocation evidence, a refused launch in the evidence that recorded the attempt as not created.
The check relies on the gateway's own report: a gateway that reports a capability it does not implement passes, and Heartwood does not test the deletion behavior of the registered gateway.
The capability names come from this build's patches; no upstream release reports them, so an unmodified upstream gateway is refused.

#### Behavior

The revocation and cleanup evidence depends on these behaviors, which the unmodified upstream release at the same revision lacks:

- The Docker driver rejects deletion of a sandbox whose provisioning has not settled, instead of abandoning the provisioning task; without it, a container request already sent to Docker can produce a worker after the gateway reported the sandbox deleted and Heartwood recorded its absence.
  The driver reports it as capability `openshell.compute.delete-awaits-provisioning`.
- An explicit delete of a sandbox already marked as deleting calls the driver again; without it, a failed deletion stays marked as deleting, later requests are acknowledged without cleanup, and revocation times out without evidence of absence.
  The gateway reports it as capability `openshell.compute.delete-retries-deleting` on its compute driver.
- The Kubernetes driver reports a delete that the Kubernetes API answers with a conflict as an error, because the Sandbox resource may still exist; the upstream release reports that delete as absence.
  A missing Sandbox resource is still reported as absent.
  The driver reports capability `openshell.compute.delete-awaits-provisioning`, which Heartwood's check requires under an `openshell-kubernetes` manifest.
  Revocation through the Kubernetes driver sends such a deletion again.
- A deletion that finds no sandbox under a workspace and name records that name in the gateway's store and answers `ALREADY_ABSENT`.
  For 24 hours after the last such deletion, the gateway rejects a creation under that name before it asks the compute driver to create the sandbox, with status `FAILED_PRECONDITION` and reason `SANDBOX_NAME_CONFIRMED_ABSENT`.
  A creation request that was still in flight when the deletion answered therefore creates no sandbox.
  The record lasts across a gateway restart as long as the store does, and the gateway removes expired records.
  A deletion that finds a sandbox answers `COMPLETED`, `ACCEPTED` or an error for that sandbox and leaves its name available.
  Only `ALREADY_ABSENT` is evidence that a name is absent; `NOT_FOUND` is never evidence, whether or not the deletion recorded the name, and a deletion by a caller without the required role in the workspace records nothing.
  In a workspace that does not exist yet, only a caller who may create that workspace records the name.
  A replayed `ALREADY_ABSENT` answer renews the record, and the gateway refuses the replay once a sandbox has the name.
  A creation that the gateway recorded but had not sent to its driver when the gateway stopped is never started while its name is recorded, and the gateway's record sweep deletes it while the record is retained.
  Names longer than 19 bytes are never recorded; the gateway also refuses to create them.
  The gateway reports this behavior as capability `openshell.compute.create-rejects-deleted-name` on its compute driver.
  Heartwood records an unanswered creation as never created only when this gateway answers `ALREADY_ABSENT` to a deletion of the attempt's name.

Heartwood sends the worker attempt's identifier as the `request_id` of its creation request.
Within the gateway's replay window, counted from the completion of the first delivery, the gateway executes that request once: a repeated delivery receives the original result while the sandbox exists and is refused after its deletion.
A creation delivered again after a revocation within that window therefore creates no sandbox.
The window is the gateway setting `mutation_replay_window_seconds`, 24 hours by default, the same time for which a name confirmed absent stays rejected; a repeated delivery after the window is executed again, so do not shorten it.
This request admission needs the gateway's JWT signing key, or its TLS private key when it has no JWT signing key, to be readable, and the same on every gateway that shares a store; a rotated key makes earlier requests unreplayable.
It also needs a caller with a user identity, such as the mutual TLS client identity.
Otherwise every creation is refused before any work.
Each admission holds one of the caller's admission slots, `mutation_admission_limit_per_caller` with a default of 1,000.
A completed creation holds its slot until its replay window ends.
A creation whose outcome the gateway did not record, such as a failed creation or a creation refused under a name confirmed absent, holds its slot for as long as the gateway's store keeps it, and OpenShell provides no operation that releases it.
The slots count every mutation with a request identifier from the same caller identity, also those of other clients that use the identity Heartwood uses at the sandbox gateway.
When the slots are used up, the gateway refuses every such request with `RESOURCE_EXHAUSTED` before any work; it gives the same status in other cases before any work, such as while its admission workers are busy.
Size the limit for the completed creations within the window plus every creation without a recorded outcome over the lifetime of the gateway's store.
The reference platform sets `mutation_admission_limit_per_caller = 100000` in each session's sandbox gateway, because one researcher and project use one gateway and its store for the life of the session, and every creation without a recorded outcome stays counted.

Some cases keep a name rejected until its record expires although a deletion found its sandbox: two concurrent deletions that both find the sandbox, contention on the record that outlasts the gateway's eight attempts, a store outage longer than the gateway's retries, an error on the deletion's second read, and a gateway crash between the record and its withdrawal.
A sandbox that passed its own check and later meets the record of a deletion that found it cannot be started until that record expires.
These cases delay a name or a sandbox until the record expires and never report a sandbox absent.
Gateway clocks that differ by more than 5 minutes, or that jump, can end a record's 24 hours early; a creation still in flight can then create a sandbox after its name was confirmed absent, so gateways that share a store need synchronized clocks.
The gateway removes at most 1,000 expired records per minute, so a caller that deletes absent names faster than that grows the store.

#### Update Policy

The Heartwood maintainers own the sandbox gateway build and its security updates.
They rebuild it with a new local version for a security fix in the upstream gateway, its Rust dependencies, the build toolchain or the base image, for a change of a patch, and for a new upstream release that the project adopts.
Each rebuild passes native qualification on both drivers before a release names it.
A patch is removed when an upstream release provides its behavior, following the removal rule in [Testing and Evidence](../architecture/testing.md#candidate-builds); the build is removed when no patch remains, and deployments then use the upstream release.
Report a security concern about the build through the repository [security policy](https://github.com/SchmiedmayerLab/heartwood/security/policy).

### Sensitive Data

Sensitive-data use requires institution-approved data eligibility, provider agreements, residency, retention, monitoring and output-release controls for the exact deployment.
Heartwood provides selected-input staging and read-only mounts, not quarantine or steward-approved release.
Managed evidence in `required` mode blocks a reviewed action while signing or retained-history verification is unavailable; `buffered` mode lets ordinary work continue with a visible gap, but outside operations and result releases still wait for retained evidence.
A successful synthetic sandbox run is not sensitive-data approval.

## Information to Return

| Item | Requested information |
|---|---|
| Scope | Hosting, isolation, inference, metadata, workflow execution, output publication and budget capabilities, each selected independently |
| Environment | Deployment, cloud, region, synthetic project identifier and technical contact |
| Identity | Principal type, audience and scopes, project permissions, token lifetime, secret reference and revocation mechanism |
| APIs | Base URLs, specification versions, selected operations, request limits, pagination, retry and error semantics |
| Inference | Model identifiers and protocols, permitted model set, data eligibility, declared data handling per connection, permitted data destinations, quota and retention terms |
| Storage | Research and state locations, project identity binding, locking, backup and restore, input and output controls |
| Network | Proxy origin and base path, gateway access restrictions, approved egress and its enforcement |
| Compute | Worker architecture, isolation mechanism, sandbox gateway version, image policy and resource limits |
| Operations | Workflow or app revisions, billing binding, status and cancellation behavior, lost-response reconciliation |
| Records | Signer endpoint and identity, verification keys, retention destination and policy, publisher and verifier permissions, restore procedure |
| Evidence | Synthetic test procedure, audit correlation, revocation checks, known limitations and the institutional approval owner |

Send identifiers and secret-manager references through the integration documentation and credentials through the approved secret channel.
Never include real research data or credentials in examples, tickets or qualification artifacts.

## Contract Bundle and Acceptance

[Export the contract bundle](platform-contract.md#generate-and-review-the-current-artifacts) and distribute the complete directory with these requirements, the [Platform Contract](platform-contract.md), the [Gateway API reference](../reference/gateway-api.md) and the exact source revision or release digest.
The bundle supports integration and access review; it does not assert that draft services are deployed.

Check captured model and service responses with the [offline conformance checks](platform-contract.md#check-platform-service-responses) and the inference and MCP endpoints with the [live endpoint checks](platform-contract.md#check-live-endpoints), then qualify the artifact in the synthetic environment.
For each selected capability, return an authorized control and its rejected alternative; a failed request alone can indicate a broken service rather than effective policy.

| Capability | Authorized control | Rejected or interrupted control |
|---|---|---|
| Gateway access | The owning researcher reaches the declared route and reconnects to the event stream | Another principal, an invalid capability, a wrong origin or a direct upstream connection is refused |
| Inference | The approved model and identity complete a bounded synthetic tool call | A disallowed model or tenant, direct credential access and a revoked worker route fail |
| Data access | A selected synthetic input is readable through its staging route | Unselected resources, writes to read-only inputs and unauthorized export fail |
| Workflow execution | The exact reviewed request yields a correlated native operation | Changed inputs or project, expired approval and conflicting replay fail; a lost reply reconciles without resubmission |
| Retained evidence | An authorized checkpoint is readable and verifies after restore | Wrong-scope signing, unauthorized publication, replacement and deletion fail; a `required`-mode outage prevents dispatch |
| Worker lifecycle | The outside controller admits and then revokes the exact attempt | The worker cannot reach protected state or management authority; uncertain deletion does not release ownership |

For capabilities without a Heartwood runtime integration, platform-side evidence establishes only the native service behavior.
Record the result against the deployment following [Validate a Platform](platform-contract.md#validate-a-platform).
