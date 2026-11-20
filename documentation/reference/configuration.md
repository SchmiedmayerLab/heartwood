<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Configuration and State Reference

Heartwood derives the project from the process current directory.
No environment variable selects a Heartwood home, workspace, state root, model root, or session directory.

## Project State Layout

A local launch keeps project state in `.heartwood/` inside the research directory.
`heartwood gateway serve --deployment` and `heartwood project migrate` and `recover-migration` instead use the `state_root` that the [deployment manifest](#deployment-manifest) binds; the layout below is the same.
No other command selects the external state, and research-directory content cannot select or replace it.

| Path | Contents |
|---|---|
| `config.toml` | Non-secret project configuration; see [Project Configuration](#project-configuration) |
| `state.json` | State-schema marker with the version of each persisted record type |
| `sessions/` | Session metadata, events, audit chains, command receipts, recovery journals, and OpenHands persistence |
| `models/` | Downloaded or bundle-imported model artifacts and their provenance |
| `skills/` | Installed project Skills |
| `audit/` | Project-level audit artifacts |
| `runtime/` | Managed runtime process and readiness state, including `managed-inference.pending.json` |
| `logs/` | Managed inference and gateway diagnostics |
| `cache/` | Project-scoped model and runtime caches |

Heartwood creates these with owner-only permissions and rejects symbolic-link substitutions.
A `.gitignore` inside local `.heartwood/` excludes every state file from the surrounding Git repository.
Heartwood applies only registered forward migrations, under a process-shared lock, and replaces migrated records atomically; unknown versions and malformed records fail closed.
Lock files such as `.config.lock`, `.state.lock`, and each session's `.writer.lock` hold ownership; never delete them.
Protected external state must never be mounted into an isolated worker.

## Project Configuration

Change configuration through the CLI or browser settings, not by editing `config.toml`.
Writes are validated, atomic, and serialized by `.config.lock`.
The file never contains a raw credential.

| Key | Contents |
|---|---|
| `schema_version` | `heartwood.project-config.v2` |
| `platform_id` | Detected platform: `generic`, `terra`, or `carina` |
| `model_source` | Selected connection identifier, such as `heartwood` or `openai` |
| `[action]` | Action-review selection; see [Action Review Settings](#action-review-settings) |
| `[audit]` | Optional `signer_profile` approved by the deployment signer registry |
| `[models]` | Non-secret model profiles and `active_profile` |
| `[[connections]]` | Platform-provided model connections |
| `[[mcp_servers]]` | Research MCP servers: identifier, label, URL, enabled state and the reviewed definition of each selected tool; no credential |
| `[local_model]` | Selected Heartwood-managed model artifact with its path, source, license, runtime settings, and qualification record |
| `[policy]` | Project policy; see [Policy Fields](#policy-fields) |

Any other key is rejected.
Select the audit signer with `heartwood audit signer select` or `default`; signer endpoints, keys, and tokens are never project configuration.

### Action Review Settings

```toml
[action]
schema_version = "heartwood.action-settings.v1"
review_mode = "review-changes"
```

`review_mode` is `review-all`, `review-changes`, or `review-outside-effects`.
When it is absent, sessions in an isolated worker use `review-outside-effects` and all other sessions use `review-all`.
The effective mode is `review-all` whenever the selection is outside `allowed_action_review_modes`, needs an isolated worker the session does not have, or reduces review outside an isolated worker while the model credential is not isolated from the tool runtime.
Under a deployment manifest, a stored `review_mode` that the manifest's ceiling no longer permits is replaced by `review-all` when the project loads, as a narrowed destination list is applied.
The change is recorded in each session before its next model dispatch, `heartwood doctor`, the browser and `gateway serve` report it as [`HW-SETUP-003`](troubleshooting.md#hw-setup-003-stored-review-mode-is-not-permitted-by-the-deployment) with the stored and effective modes, and selecting any permitted mode replaces the stored one.

The selection has one of two scopes, reported as `scope` by `GET /settings/actions` and `heartwood actions`:

| Scope | Where the selection lives |
|---|---|
| `project` | `[action]` in the deployment's protected `config.toml`, shared by every interface and applied to the next action set |
| `launch` | Gateway memory for a local launch; it ends with the process and `review_mode` is never written to `.heartwood/` |

Outside operations and result releases always wait for the researcher, in every mode.

### Policy Fields

`[policy]` in `config.toml` and in the deployment manifest uses the `heartwood.policy-profile.v1` fields:

| Field | Default | Meaning |
|---|---|---|
| `policy_id` | required | Policy identifier |
| `platform_id` | required | Must equal the detected platform |
| `deny_egress_by_default` | `true` | Must be `true` under a deployment manifest |
| `allowed_model_endpoints` | `[]` | Exact model request endpoints |
| `allowed_model_catalog_endpoints` | `[]` | Exact model catalog endpoints |
| `allowed_capability_tiers` | `["supervised"]` | Any of `autonomous`, `supervised`, `experimental` |
| `allowed_action_review_modes` | all three modes | Review-mode ceiling; must list `review-all` exactly once |
| `credential_allowlist` | `[]` | Credential binding names the policy permits |
| `aggregate_count_floor` | `20` | Minimum aggregate count; at least `20` |
| `allowed_credential_backends` | all four backends | Any of `process`, `keyring`, `mounted-file`, `managed-identity` |
| `allowed_checkpoint_signer_profiles` | none | Optional list of signer registry profiles a project may select; none leaves selection to the deployment registry |
| `allowed_data_destinations` | none | Optional list of permitted [declared data destinations](../models/index.md#data-and-compliance-boundary); a route without a declaration is then refused at selection and before each model call |
| `notes` | none | Free text |

The built-in platform policies for local launches permit `review-all` and `review-changes`.
Under a deployment manifest the project policy can only narrow the manifest's: endpoints, tiers, review modes, credential names, credential backends, signer profiles, and data destinations must be subsets, a deployment's data-destination list cannot be removed, and the aggregate floor cannot be lower.
The effective review ceiling is the intersection of the two lists; a project list equal to the manifest's is not stored, so a later manifest change applies.
The gateway applies a manifest's `allowed_data_destinations` every time it loads the project, so adding or changing it applies to existing projects at the next start:
a project without its own list uses the manifest's, and a project list keeps only the destinations both permit.
A project list that shares no destination with the manifest's stops the gateway from loading the project until one of them changes.
An active route outside the list stays selected, but model validation reports it as denied and each model call is refused until a permitted route is selected.
`heartwood doctor` and the browser readiness view report such a route as [`HW-SETUP-002`](troubleshooting.md#hw-setup-002-model-and-policy-settings-do-not-agree) with the policy's reason.

## Deployment Manifest

An operator deployment manifest binds one research directory to protected external state, a policy ceiling, and optionally a managed worker and checkpoint evidence.
Only a trusted launcher passes it, as `heartwood gateway serve --deployment PATH` or to the [migration commands](cli.md#project-state-migration).

```toml
schema_version = "heartwood.project-deployment.v1"
project_id = "93d29ccd-6177-4817-9806-44ea579818c9"
workspace = "/research/project"
state_root = "/var/lib/heartwood/projects/93d29ccd-6177-4817-9806-44ea579818c9"

[policy]
policy_id = "deployment-models"
platform_id = "generic"
deny_egress_by_default = true
allowed_model_endpoints = ["https://models.example.invalid/v1/chat/completions"]
allowed_model_catalog_endpoints = ["https://models.example.invalid/v1/models"]
allowed_capability_tiers = ["supervised"]
allowed_action_review_modes = ["review-all"]
credential_allowlist = ["HEARTWOOD_CUSTOM_MODEL_API_KEY"]
allowed_credential_backends = ["mounted-file"]
allowed_checkpoint_signer_profiles = ["research-records"]
allowed_data_destinations = ["same-compute", "institution-managed"]
aggregate_count_floor = 20

[requirements]
isolated_worker = false
checkpoint_evidence = false
```

| Key | Requirement |
|---|---|
| `schema_version` | `heartwood.project-deployment.v1` |
| `project_id` | Canonical lowercase UUID |
| `workspace` | Exact research directory; Heartwood must be started from it |
| `state_root` | Absolute protected state directory outside the research directory, whose final component is `project_id`; its parent must exist |
| `[policy]` | Required; must narrow the detected platform adapter's policy, as described in [Security](../operate/security.md#deployment-policy); see [Policy Fields](#policy-fields) |
| `[requirements]` | Optional; `isolated_worker` and `checkpoint_evidence` set to `true` stop the launch with `HW-DEPLOY-003` unless the manifest configures `[worker]` or required-mode `[evidence]` |
| `[worker]` | Optional managed worker; see [Managed Worker](#managed-worker) |
| `[evidence]` | Optional automatic checkpointing; requires `[worker]`; see [Configure Managed Evidence](../operate/audit-checkpoints.md#configure-managed-evidence) |
| `[model_connections]`, `[model_metadata]` | Optional platform model connections and model declarations; see [Model Connections](#model-connections) |
| `[[mcp_destinations]]` | Optional origins for research MCP servers; requires `[worker]`; see [MCP Destinations](#mcp-destinations) |
| `[identity]` | Optional OpenID Connect client and researcher; required by `identity-proxy` ingress and refused with every other mode; see [Identity](#identity) |
| `[planning]` | Optional planning service and platform launch context; requires `[identity]`; see [Planning Service](#planning-service) |

Any other key, at any level, is rejected.
The manifest must be an absolute path that traverses no symbolic link, lie outside the research directory and the state root, be a regular file of at most 64 KiB owned by root or the gateway user, and not be writable by group or others.
Every parent directory of the manifest and of `state_root` must be owned by root or the gateway user and not be group- or world-writable, except a root-owned sticky directory such as `/tmp`.
An omitted `allowed_action_review_modes` permits the platform's modes, plus `review-outside-effects` with a `[worker]` section, which makes `review-outside-effects` the default for sessions in an isolated worker; list the modes explicitly.
Credential names are references; credentials never belong in the manifest.
See [Projects and Private State](../start/project.md#operator-bound-external-state) for the operator workflow.

### Identity

The `[identity]` table binds a gateway that runs with `--ingress-mode identity-proxy` to one OpenID Connect provider, one confidential client and one researcher.

```toml
[identity]
issuer = "https://id.example.org/realms/research"
client_id = "hw-proj-demo"
client_secret_file = "/etc/heartwood/identity/client-secret"
actor_key_file = "/etc/heartwood/identity/actor-key"
subject = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
```

| Key | Requirement |
|---|---|
| `issuer` | Exact `iss` value of the provider's tokens and the origin the browser is sent to for sign-in |
| `client_id` | The gateway's client at the provider; accepted tokens name it in `aud` |
| `client_secret_file` | Absolute file that holds the client secret: printable ASCII without spaces, at most 4096 bytes, one trailing newline allowed |
| `actor_key_file` | Absolute file that holds the deployment's actor key: a UTF-8 file of at most 4096 bytes whose content without one trailing newline is the key, at least 32 bytes long; the gateway does not start without it |
| `subject` | The only `sub` value the gateway accepts, exactly as the provider issues it: 1 to 255 characters without control or format characters and without leading or trailing whitespace |
| `provider_url` | Optional base URL where the gateway itself reaches the provider's discovery document, token endpoint and keys; default `issuer` |
| `ca_file` | Optional absolute PEM file of certificate authorities that replaces the default certificate bundle for requests to the provider |

Commands record an actor derived from `issuer` and `subject` under the actor key, as [Researcher Identity](../operate/security.md#researcher-identity) describes, so a change of either value or of the key changes the actor of later records.
`issuer` and `provider_url` are `http` or `https` URLs without credentials, query, fragment, whitespace or control characters.
Both use `https`, or `http` to a loopback address.
A provider whose certificate comes from a private authority, such as a platform CA, is trusted through `ca_file`.
The gateway reads `<provider_url>/.well-known/openid-configuration`, and the document's `issuer` must equal the configured `issuer`.
The document's endpoints must lie on the origin of `issuer` or of `provider_url`.
The gateway sends the browser to the authorization endpoint on the `issuer` origin and requests tokens and keys on the `provider_url` origin.
`client_secret_file`, `actor_key_file` and `ca_file` follow the rules for operator files: outside the research directory and the state root, no symbolic link in the path, owned by root or the gateway user, and not writable by group or others.
`client_secret_file` and `actor_key_file` must also be unreadable by group and others.
[Researcher Identity](../operate/security.md#researcher-identity) describes sign-in and token handling.

### Managed Worker

The `[worker]` table runs each session's agent in an OpenShell sandbox.
Its `driver` selects the sandbox driver and the keys that belong to it; the example uses the Docker reference driver.
Replace the example identities and digests with independently qualified values; the effective policy digest must not be learned from the worker whose admission it decides.

```toml
[worker]
driver = "openshell-docker"
gateway = "research-workers"
native_config_home = "/etc/heartwood/native"
openshell_cli = "/opt/heartwood/bin/openshell"
docker_cli = "/opt/heartwood/bin/docker"
docker_host = "unix:///run/docker.sock"
workspace = "research"
image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
sandbox_policy_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
policy_path = "/etc/heartwood/native/worker-policy.json"
policy_file_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
profile_id = "approved-research"
model = "provider/approved-model"
model_endpoint = "https://models.example.invalid/v1/chat/completions"
provider = "approved-research"
placeholder_env = "HEARTWOOD_CUSTOM_MODEL_API_KEY"
cpu = "1"
memory = "2Gi"

[worker.inputs]
schema_version = "heartwood.worker-inputs.v1"
files = []
```

| Key | Requirement |
|---|---|
| `driver` | `openshell-docker` or `openshell-kubernetes`; see [Kubernetes Driver](#kubernetes-driver) |
| `gateway` | Name of the registered sandbox gateway |
| `native_config_home` | Contains `openshell/gateways/<gateway>/metadata.json` and `mtls/ca.crt`, `mtls/tls.crt`, and an owner-only `mtls/tls.key` |
| `openshell_cli` | Absolute executable path |
| `docker_cli` | Absolute executable path; `openshell-docker` only |
| `docker_host` | `unix://` path of the local Docker socket; no TCP endpoint is accepted; `openshell-docker` only |
| `docker_socket_group` | Optional numeric group ID; required when the socket is group-writable; `openshell-docker` only |
| `workspace` | Sandbox workspace name |
| `image_digest` | `sha256:` digest of the qualified worker image |
| `sandbox_policy_sha256` | Digest of the effective sandbox policy the gateway must observe |
| `policy_path`, `policy_file_sha256` | Native sandbox-policy JSON file and the SHA-256 of its contents |
| `profile_id`, `model`, `model_endpoint` | Must match the selected application model profile; the endpoint must be HTTPS and allowed by `[policy]` |
| `provider` | Sandbox inference provider attachment name |
| `placeholder_env` | Environment name under which the worker receives a credential placeholder, never the real credential |
| `cpu`, `memory` | Resource limits such as `1` or `500m`, and `512Mi` or `2Gi` |
| `[worker.inputs]` | Required, with `schema_version = "heartwood.worker-inputs.v1"`; `files` lists project files by `path`, `sha256`, and `size_bytes` to copy into the sandbox, at most 128 files, 16 MiB each and 64 MiB in total, and an empty list transfers none |

`gateway`, `workspace`, and `provider` use lowercase letters, digits, and hyphens.
Every path is absolute and canonical, outside the research directory and state root, and every native tool, configuration, policy file, and certificate is owned by root or the gateway user, not group- or world-writable, and singly linked.
The socket must be owned by root or the gateway user and not world-writable; a group-writable socket needs `docker_socket_group`, and when that is set the socket's group must match it.
Membership in the socket's group grants Docker control-plane authority; never add a worker identity to it.
The registered sandbox gateway must report the three [capabilities](../operate/platform-requirements.md#sandbox-gateway) native revocation depends on; the gateway version is not configured and does not satisfy the check.
The inference endpoint must enforce approved models and tenant scope itself; the manifest adds no model router.

#### Kubernetes Driver

With `driver = "openshell-kubernetes"` the table omits the three `docker_` keys, adds six keys, and limits the length of `workspace`:

```toml
[worker]
driver = "openshell-kubernetes"
kubectl_cli = "/opt/heartwood/bin/kubectl"
kubeconfig = "/etc/heartwood/native/kubeconfig"
namespace = "research-sandboxes"
image_repository = "registry.example.invalid/heartwood/worker"
supervisor_image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
sandbox_image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
```

| Key | Requirement |
|---|---|
| `kubectl_cli` | Absolute executable path of `kubectl` |
| `kubeconfig` | Absolute path of the `kubectl` configuration for the cluster, with a current context and the cluster's certificate authority; accessible by its owner only |
| `namespace` | Namespace in which the sandbox gateway creates the session's pods; lowercase letters, digits, and hyphens |
| `image_repository` | Registry repository of the worker image, with its registry host and without a tag or digest; the host is a dotted name, `localhost`, or a single name with a port; pods reference the image as `<image_repository>@<image_digest>` |
| `supervisor_image_digest` | `sha256:` digest of the OpenShell supervisor image |
| `sandbox_image_digest` | `sha256:` digest of the OpenShell sandbox runtime image |
| `workspace` | At most 19 characters, the limit of the OpenShell command-line tool with this driver; `hw-staging` and names that begin with `hw-staging-` are refused, because the workload pods of such a workspace would carry the name of a staging pod |

A key of the other driver is rejected with `HW-DEPLOY-001`.
With this driver, `image_digest` is the registry digest by which a pod references the worker image; with `openshell-docker` it is the image identifier the Docker engine reports.
`kubectl_cli` and `kubeconfig` follow the same ownership and location rules as the other native tools and configuration files.
The registered sandbox gateway must report one `openshell/kubernetes` compute driver with the three required capabilities.
The policy file must also list `/workspace` under `filesystem.read_write`, because the driver confines the worker to the listed paths and the worker's working directory is `/workspace`.
The credential in `kubeconfig` needs these permissions in `namespace` and no others: create, delete, get, and list pods and persistent volume claims, create `pods/exec`, get and list NetworkPolicies, get and list `sandboxes.agents.x-k8s.io`, and get the namespace.
The namespace must provide what [staging](../architecture/system.md#kubernetes-worker-staging) uses: a service account `hw-staging`, a storage class `heartwood-attempt`, and NetworkPolicies that deny the staging pod all traffic.
`kubectl` runs with `PATH` and a temporary home directory only, so a `kubeconfig` that calls a credential plugin is not supported.

An optional `[worker.project_data]` table declares a read-only volume of the project's data that the platform filled before the gateway started:

```toml
[worker.project_data]
claim_name = "hw-project-data-0123456789abcdef"
claim_uid = "6f1b8c2e-4d3a-4b5c-9e8f-0a1b2c3d4e5f"
project = "demo"
content_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
mount_path = "/data"
```

| Key | Requirement |
|---|---|
| `claim_name` | Name of the persistent volume claim in `namespace` |
| `claim_uid` | UID the cluster gave the claim; a claim created again under the name has another UID |
| `project` | The platform project the claim belongs to, which its label `heartwood.platform/project` names |
| `content_sha256` | Digest of the claim's content, which its annotation `heartwood.platform/content-sha256` names |
| `mount_path` | Absolute, normalized path at which the worker reads the data; it may not be `/` and may not lie inside or contain `/inputs`, `/skills`, `/workspace`, `/sandbox`, `/opt` or `/tmp` |

The driver configuration attaches the claim read-only on the volume and on the mount, and [admission](../architecture/system.md#kubernetes-worker-admission) refuses a worker whose volume differs from the declaration.
The policy file must list `mount_path` under `filesystem.read_only` and not under `filesystem.read_write`.
The claim must carry the OpenShell approval labels for `workspace`, which the platform sets.
The declaration is part of each attempt's input identity.

### Model Connections

The `[model_connections]` table supplies platform model connections in the [`heartwood.model-connections.v1`](../operate/platform-contract.md#contract-artifacts) format.
The optional `[model_metadata]` table declares the protocol, limits and tool support of models in the `heartwood.platform-model-metadata.v1` format, for model aliases the OpenHands model table does not recognize.

```toml
[model_connections]
schema_version = "heartwood.model-connections.v1"

[[model_connections.connections]]
connection_id = "research-models"
label = "Research Models"
protocol = "openai-compatible"
model_prefix = "openai/"
credential_kind = "file"
api_key_file = "/run/secrets/research-models-key"
base_url = "https://models.example.invalid/v1"
catalog_endpoint = "https://models.example.invalid/v1/models"
policy_endpoint = "https://models.example.invalid/v1/chat/completions"

[model_connections.connections.data_handling]
destination = "institution-managed"
retention_authority = "institution"
route = "Institution inference gateway, then the providers it routes to"

[model_metadata]
schema_version = "heartwood.platform-model-metadata.v1"

[[model_metadata.models]]
connection_id = "research-models"
model_id = "research-coder"
protocols = ["chat"]
context_window_tokens = 131072
max_output_tokens = 8192
supports_tool_calls = true
```

Each connection appears with the institution-managed providers in browser settings, `heartwood models list` and `heartwood setup --deployment MANIFEST`.
A connection identifier must use lowercase letters, digits, hyphens and underscores and must differ from Heartwood's built-in connection and model-source identifiers, because it becomes the project's model source once a model is selected.
A declared model with a supported protocol is selected at the `supervised` capability tier.
The gateway validates connections as described in [Inference](../operate/platform-contract.md#inference).
Each declaration must name a connection in `[model_connections]` and declare its model once, and `[model_metadata]` requires `[model_connections]`.
`[policy]` must still permit each connection's endpoints, credential reference and data destination.
Credential references name a variable or mounted file; the manifest never holds the credential.
An `openai` or `openai-compatible` connection with an `https` catalog endpoint may name `ca_file`, an absolute PEM file of certificate authorities that replaces the default certificate bundle for model discovery, such as a platform CA; it follows the rules for operator files and must load as a certificate authority file.
An invalid table stops the launch with [`HW-DEPLOY-001`](troubleshooting.md#hw-deploy-001-deployment-manifest-is-unavailable-or-invalid).

### MCP Destinations

Each `[[mcp_destinations]]` entry allows one origin for [research MCP servers](../operate/platform-contract.md#research-mcp-servers).

```toml
[[mcp_destinations]]
service_id = "research-catalog"
origin = "https://catalog.example.invalid"
tools = ["find_records"]
observation_tool = "get_operation"
max_result_bytes = 16384
call_timeout_seconds = 30
private_address = false
```

| Key | Requirement |
|---|---|
| `service_id` | The service identity the service reports in its operation observations; it also names the token binding |
| `origin` | Scheme, host and optional port only; HTTPS except on loopback; no private address without `private_address`, and never a link-local or metadata address |
| `tools` | Tools researchers may select on servers at this origin |
| `observation_tool` | Read-only tool that reports an operation by `project_id` and `operation_id`, and an identifier it never received as `failed`; not selectable |
| `max_result_bytes` | Optional result limit from 1024 to 32768 bytes; default 16384 |
| `call_timeout_seconds` | Optional limit for one request, from 1 to 300 seconds; default 30 |
| `private_address` | Optional; `true` permits an RFC 1918 or unique local address for this exact origin, such as a cluster service; default `false`. Link-local and metadata addresses stay refused, and HTTPS stays required except on loopback. |
| `ca_file` | Optional absolute PEM file of certificate authorities, such as a platform CA, that replaces the default certificate bundle for this origin; an `https` origin only; follows the [rules for operator files](#identity) |
| `token_exchange` | Optional table with `scope` and `audience`; the gateway then obtains this service's token by exchange for the signed-in researcher's token; requires `[identity]` |

A destination with a `token_exchange` table takes no entered, stored or operator-supplied token.

```toml
[[mcp_destinations]]
service_id = "research-data"
origin = "https://data.example.invalid"
tools = ["get_object"]
observation_tool = "get_operation"

[mcp_destinations.token_exchange]
scope = "svc-data"
audience = "heartwood-data"
```

| Key | Requirement |
|---|---|
| `scope` | The one scope the identity provider requires for this service |
| `audience` | The service's audience at the identity provider |

[Service Tokens](../operate/security.md#service-tokens) describes the exchange.

A platform job service adds an `operations` table to its entry; see [Platform Job Operations](../operate/platform-contract.md#platform-job-operations).

```toml
[[mcp_destinations]]
service_id = "research-jobs"
origin = "https://jobs.example.invalid"
tools = ["create_task", "cancel_task"]
observation_tool = "get_task"

[mcp_destinations.operations]
capabilities_tool = "describe_operations"
proposal_tool = "propose_task"
cancel_tool = "cancel_task"
```

| Key | Requirement |
|---|---|
| `capabilities_tool` | Read-only tool that declares the service's operations, their permissions and its optional capabilities; not selectable |
| `proposal_tool` | Read-only tool that describes a submission before review; not selectable |
| `cancel_tool` | Optional; a selectable tool that cancels an earlier operation named by `target_operation_id`; every other selectable tool submits work |

Each origin may appear once, and the section requires `[worker]`.
Without the section, every research MCP server is refused.
A changed destination list counts as a manifest change beyond its policy, which stops dispatch until the next start.

### Planning Service

The `[planning]` table connects the gateway to a platform planning service, which reports budgets and estimates for the [planning projection](../operate/platform-contract.md#planning-against-platform-budgets).

```toml
[planning]
url = "https://heartwood-control.heartwood-platform.svc.cluster.local:8443/mcp"
ca_file = "/etc/heartwood/deployment/platform-ca.crt"
private_address = true
launch_context_file = "/etc/heartwood/deployment/launch-context.json"

[planning.token_exchange]
scope = "svc-control"
audience = "heartwood-control"
```

| Key | Requirement |
|---|---|
| `url` | HTTPS URL of the service's MCP endpoint, without credentials, query or fragment; no private address without `private_address`, and never a link-local or metadata address |
| `ca_file` | Optional absolute PEM file of certificate authorities that replaces the default certificate bundle for the service; follows the [rules for operator files](#identity) and must load |
| `private_address` | Optional; `true` permits an RFC 1918 or unique local address, such as a cluster service; default `false` |
| `launch_context_file` | Absolute operator file of at most 64 KiB with the `heartwood.platform-launch-context.v1` document the platform issued for this gateway; follows the [rules for operator files](#identity) |
| `token_exchange` | Table with `scope` and `audience`; each call carries a token exchanged for the signed-in researcher's token |

Without a live sign-in the planning service counts as unavailable, and an exchange the identity provider refuses counts as refused.
The gateway reads the launch context once at start, and a changed `[planning]` table stops dispatch until the next start.

## Credential Binding Names

Built-in connections use binding names such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `STANFORD_AI_API_KEY`, and `HEARTWOOD_CUSTOM_MODEL_API_KEY`.
A research MCP server's token uses `HEARTWOOD_MCP_TOKEN_` followed by its destination's `service_id` in upper case, with other characters than letters and digits as underscores, such as `HEARTWOOD_MCP_TOKEN_RESEARCH_CATALOG`.
A binding name identifies a secret source; it is not the secret.
On a workstation, a value saved with `--remember-credential` is stored in the operating-system keyring under a project-scoped account.
Containers and platform adapters use process entry, a mounted secret file, or a managed identity.
An isolated worker receives only the inference placeholder; real credentials and their sources stay outside its environment and mounts.

## ChatGPT Account State

**Sign in with ChatGPT** delegates sign-in, token storage, refresh, model discovery, and request transport to OpenHands.
OpenHands stores the account credential in its private per-user store outside project state, so one sign-in serves every project of the same operating-system user.
Project state keeps only the non-secret connection and selected model.
A disposable container needs a private credential-directory mount to keep the sign-in after it exits.
Remove the credential with `heartwood models forget openai-subscription` or **Sign out** in browser settings.

## Deployment Environment

Environment variables are operator inputs at platform boundaries: platform detection, provider credentials, Jupyter routing, scheduler identity, GPU visibility, and packaged runtime wiring.
Never write real secret values into shell history or documentation.

| Variable | Effect |
|---|---|
| `HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY` | Absolute path of the one checkpoint signer registry to use |
| `HEARTWOOD_SKILL_SOURCES_FILE` | Absolute path of the one Skill source registry to use |
| `HEARTWOOD_TERMINAL_ENV_ALLOWLIST` | Comma-separated extra variable names admitted into local agent terminals; see [Security and Controlled Data](../operate/security.md) |
| `HEARTWOOD_REVIEW_QUALIFICATIONS` | Operator-owned retained review evidence; see [Parallel Reviews](../operate/parallel-reviews.md) |
| `HEARTWOOD_GATEWAY_CAPABILITY` | Rejected; deliver the capability with [`--capability-fd`](cli.md#gateway-serve) |

### Checkpoint Signer Registry

Heartwood reads exactly one registry: `HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY` when set, otherwise `/etc/heartwood/checkpoint-signers.toml`, otherwise `heartwood/checkpoint-signers.toml` under `XDG_CONFIG_HOME` or `~/.config`.
Registries are never merged.
See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md).

### Skill Source Registries

Signed Skill sources are deployment configuration, not project state.
Heartwood reads exactly one registry: `HEARTWOOD_SKILL_SOURCES_FILE` when set, otherwise `/etc/heartwood/skill-sources.toml`, otherwise `~/.config/heartwood/skill-sources.toml`.
Relative `trusted-root` and `repository` paths resolve against the registry's directory.

```toml
schema_version = "heartwood.skill-sources.v1"

[[sources]]
id = "institution"
kind = "remote"
trusted-root = "/etc/heartwood/trust/institution-skills-root.json"
metadata-url = "https://skills.example.edu/metadata/"
targets-url = "https://skills.example.edu/targets/"

[[sources]]
id = "institution-offline"
kind = "offline"
trusted-root = "/approved-media/skills/metadata/1.root.json"
repository = "/approved-media/skills"
```

A `remote` source takes HTTPS `metadata-url` and `targets-url` without credentials, query, or fragment; an `offline` source takes a `repository` holding `metadata/` and `targets/`.
`enabled = false` disables a source.
The TUF trusted root must arrive through an independently trusted channel; mount it and the registry read-only, outside Heartwood's writable metadata cache.
Heartwood verifies signatures, metadata versions and expiry, target length and digest, catalog policy, the archive manifest, and the extracted Agent Skill before installation.

Only `/etc/heartwood/skill-sources.toml` may approve exact Skill trees for controlled data, per source:

```toml
controlled-data-approved-digests = [
  "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
]
```

Values are unique 64-character SHA-256 tree digests; a registry selected any other way that contains them is rejected.
Do not copy the example digest, and do not populate the list from repository review alone.
A marketplace, mutable branch, or unauthenticated archive URL is not a Heartwood Skill source.

## Concurrency

Configuration updates are serialized across Heartwood processes.
Each active session has one writer, enforced by an operating-system lock.
Stop the process that owns a session before continuing it from another interface, or use another session identifier.
Do not delete lock, command-receipt, or recovery-journal files.

## Audit Artifacts

Session audit logs and exports are private project state.
Authoritative checkpoints, signer registries, signer credentials, private keys, and independently trusted public keys belong outside the project.
Neither `.heartwood/` nor protected external state is an authoritative archive; retain checkpoints in deployment records storage.
