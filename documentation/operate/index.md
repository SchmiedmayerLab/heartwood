<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Deploy Heartwood

A Heartwood deployment combines a pinned release artifact with the platform's storage, identity, network, secret, compute, model-route, logging and data-governance controls.
Read [Support and Compatibility](support.md) before selecting a release, and [Security and Controlled Data](security.md) before choosing an execution shape.

## Deployment Responsibilities

| Layer | Heartwood provides | Platform or institution provides |
|---|---|---|
| Application | Gateway, terminal, browser and notebook interfaces, OpenHands adapter, Skills, policy, action review, audit | Artifact approval and release selection |
| Project | Research-file boundary; local `.heartwood/` state, or protected state bound by the deployment manifest | Durable storage, permissions, backup, retention and deletion |
| Execution | Local execution, or an isolated OpenShell worker with gateway-side admission, fencing and revocation | Docker engine, sandbox gateway, mandatory filesystem and network enforcement |
| Identity | Launch capability on every gateway request, credential bindings, credential-boundary checks | User authentication and authorization, capability delivery, secret delivery, managed identities |
| Models | Provider catalogs, route-policy evaluation, Heartwood-managed inference planning | Approved endpoints, agreements, accounts, quotas and data eligibility |
| Network | Deny-by-default model-route policy and declared ingress validation | Enforced egress, authenticated ingress, proxy header sanitation, TLS and segmentation |
| Evidence | Session events, hash-chained audit, scrubbed export, signed checkpoints, deployment-bound evidence gates | Signer authorization and key custody, records storage and retention, monitoring, incident response |

The [Platform Contract](platform-contract.md) defines this boundary for platform partners, and [Platform API and Access Requirements](platform-requirements.md) lists what to request from a platform operator.

## Select an Artifact

- The standard image (`ghcr.io/schmiedmayerlab/heartwood`) is published for `linux/amd64` and `linux/arm64`.
- The NVIDIA image (`-gpu-nvidia` tags) is `linux/amd64` only; match its configuration against the [GPU compatibility matrix](../reference/gpu-compatibility.md).
- The Terra images (`-terra` and `-terra-gpu-nvidia` tags) are single-platform `linux/amd64` images for Terra Leonardo.
- The native installer and archive are for platforms where containers are not the normal execution mechanism.

Pin a release tag or digest; do not deploy the moving `edge` tags.
Releases do not publish a worker image or a sandbox gateway.
For an isolated worker, build the `worker` target of `docker-bake.hcl` from the release commit, record its image identifier, and qualify it with the sandbox gateway you deploy.

## Deployment Manifest

The deployment manifest is an operator-owned TOML file that binds one research directory to protected state, a policy ceiling and, optionally, an isolated worker and managed evidence.
[Configuration and State](../reference/configuration.md#deployment-manifest) specifies every key, with a complete example.
It is read only by `heartwood gateway serve --deployment PATH`, `heartwood project migrate --deployment PATH --backup PATH` and `heartwood project recover-migration --deployment PATH`:

```bash
heartwood gateway serve --deployment /etc/heartwood/projects/research.toml
```

Start the command from the exact research directory the manifest names.
The standalone terminal, `NotebookSession()` and `heartwood audit` commands never read a manifest, so researchers reach a bound project through the gateway's browser interface or a client of its [API](../reference/gateway-api.md).
Keep the manifest and every file it names outside the research directory and the state directory, owned by root or the gateway user and not writable by group or others; any violation, unknown key or invalid value stops startup before a store is opened.
Existing local `.heartwood/` state is never adopted silently; move it with `heartwood project migrate`, as described in [Operator-Bound External State](../start/project.md#operator-bound-external-state).

### Policy Ceiling

The `[policy]` table replaces the platform adapter's built-in policy as the ceiling for the project, and project settings can only narrow it.
`platform_id` must equal the detected platform adapter, and `deny_egress_by_default` must stay `true`.
When `allowed_action_review_modes` is omitted, the platform's modes are permitted, plus `review-outside-effects` with a `[worker]` section, and a session in an isolated worker with no researcher selection uses `review-outside-effects`.
Set the list explicitly; [Action Review](security.md#action-review) describes each mode and the evidence required before listing it.

### Isolated Worker

A `[worker]` table runs each session's agent in an OpenShell sandbox on a local Docker engine.
Without it, the agent runs in the gateway process even though state is protected.
Take `sandbox_policy_sha256` and `image_digest` from qualification, not from the worker whose admission they decide.
Membership in the Docker socket's group grants control-plane authority; never add a worker identity to it.

Before serving, and before each worker launch or reconciliation, Heartwood reads the registered sandbox gateway's info and stops with [`HW-WORKER-001`](../reference/troubleshooting.md#managed-workers) unless its Docker compute driver reports both deletion capabilities.
The qualification build's patches report them; an unmodified upstream gateway does not.
The check relies on the gateway's own report; it does not establish that the gateway has the [behavior native qualification depends on](platform-requirements.md#gateway-behavior-behind-native-qualification).

### Evidence

An `[evidence]` table binds automatic signed checkpoints to an independently authorized signer and records service.
It requires a `[worker]` table; see [Configure Managed Evidence](audit-checkpoints.md#configure-managed-evidence) for its keys.

## Persist the Right Data

Assign one dedicated project directory as the process current directory.
Persist research files and canonical state separately from installation files, temporary runtime files and job scratch.
Local launches use `.heartwood/` in the project; manifest launches use `state_root`.
Back up the research files and their matching state together, and preserve the project identity on restore.
Never mount the state directory or the manifest into a worker.

Do not pre-create `.heartwood/` with another layout.
Opening the browser and running `heartwood doctor` are read-only; project confirmation or the first mutating operation initializes state.

## Provide Model Connections

Built-in connections and operator-configured compatible routes are checked against the active policy's endpoints, credential references and capability tiers.
A deployment manifest can supply platform connections and model declarations; see [Model Connections](../reference/configuration.md#model-connections).
Never place raw tokens in container layers, image labels, project configuration, command arguments, examples or CI logs.
An isolated worker uses only the `[worker]` model route, and its real credential stays in the OpenShell provider.

## Validate the Deployment

Before real data, use a synthetic project to verify:

1. the artifact digest and the `GET /project/capabilities` response;
2. project persistence across process or compute restart;
3. model discovery and a real agent response with a structured tool proposal;
4. grouped approve and reject behavior, and that only the intended review modes are offered;
5. for an isolated worker, outside denial of protected state, control-plane access and unapproved egress, and revocation before writer release;
6. terminal, browser and notebook parity where advertised;
7. replay and scrubbed audit export with no secret values in configuration, events, logs or exports;
8. ingress host, origin, forwarding, prefix and source rejection;
9. the reported model-credential boundary;
10. enforced network behavior, including a no-network Heartwood-managed inference test when offline operation is claimed; and
11. signed checkpoint creation, external retention, independent verification and synthetic restore.

Concurrent advisory reviews require their own evidence; see [Qualify Parallel Reviews](parallel-reviews.md).
Keep validation records with the deployment's operating documentation and never include protected data in a fixture or transcript.

## Record a Standard Environment

When several researchers share one setup, keep one record of it outside every research project and verify each researcher's environment against it before project data is added.

| Record | Source |
|---|---|
| Release tag and verified digest | [Select an Artifact](#select-an-artifact) |
| Deployment manifest and its digest | [Deployment Manifest](#deployment-manifest) |
| Platform image, compute shape and storage | The platform guide, such as [Terra](../platforms/terra.md) |
| Approved model routes | [Choose a Model Route](../models/index.md#data-and-compliance-boundary) |
| Permitted review modes | [Action Review](security.md#action-review) |
| Signer profile and checkpoint retention | [Audit Checkpoints and Retention](audit-checkpoints.md) |
| Analysis dependency lock | [Verify an Existing Analysis](../use/research-workflows.md#verify-an-existing-analysis) |
| Owner and next update review | [Support and Compatibility](support.md#update-cadence) |

## Run a Pilot Evaluation

A pilot shows whether a small group can install, use and trust one standard environment before a wider rollout.
Use synthetic or approved non-sensitive data, keep the release pinned, and name a support contact.

1. Agree on exit criteria before the first participant starts.
2. Onboard each participant until they have made one action decision.
3. Have every participant run the three [research workflows](../use/research-workflows.md) in order on the same synthetic inputs.
4. Collect the evidence below, then list the changes a wider rollout or a second platform requires.

| Question | Evidence |
|---|---|
| Could participants start? | Installation success and time to the first action decision |
| Did the workflows complete and reproduce? | `heartwood experiments list --json` and the verification report |
| How was Heartwood used? | `heartwood usage summary --json` |
| Is the record intact? | `heartwood audit verify` |
| What did support require? | Requests and time to resolution |
| Would participants use it? | Structured feedback |

Collect counts and outcomes, not prompts, project files or session history.
The usage summary verifies every session's history first and reports sessions, turns, action decisions, workflow, reproduction and experiment outcomes, the release and the platform.
It contains no prompt, path, file name, session title, model output or record identifier; a random per-project summary identifier lets the operator distinguish participants' projects without identifying them.
A summary whose audit verification failed carries no counts; investigate the project with `heartwood audit verify` instead of using partial results.
A summary that reports the history as incomplete found an interrupted commit; it also carries no counts, and the summary succeeds again once Heartwood has completed the commit by opening or verifying that session.
The summary is the published `usage-summary.schema.json` artifact of the [platform contract](platform-contract.md#contract-artifacts), also available through `GET /project/usage-summary` and `NotebookSession.usage_summary()`.
Report the results as live synthetic validation of that environment; see [Testing and Evidence](../architecture/testing.md#claims).
