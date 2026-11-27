<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Diagnostics and Troubleshooting

Most setup failures are recoverable without deleting the project.
Start with the content-safe readiness report:

```bash
heartwood doctor
```

Each warning or failure carries a stable `HW-*` code, a title, a next action, and a link to its section here.
`heartwood doctor --json` prints the structured report; it can contain project paths, so review it before sharing.

## Diagnostic Code Format

Codes use `HW-{AREA}-{NNN}`.
`AREA` identifies where the condition must be resolved; it does not identify severity.

| Area | Conditions |
|---|---|
| `PROJECT` | Project boundary, storage, and private project state |
| `SETUP` | Non-secret Heartwood configuration and policy agreement |
| `MODEL` | Model selection, compatibility, and managed model files |
| `CREDENTIAL` | Provider API key, subscription credential, or managed-identity availability |
| `INGRESS` | Gateway binding, proxy trust, origins, forwarding metadata, paths, and researcher identity |
| `AGENT` | OpenHands agent runtime availability |
| `WORKSPACE` | Read-only project file and change inspection |
| `COMPUTE` | Scheduler allocation, GPU, memory, and scratch storage |
| `TERRA` | Terra-specific project and compute requirements |
| `WORKER` | Managed worker sandbox gateway |
| `MCP` | Research MCP server destinations, credentials and tool review |
| `PLAN` | Research planning against platform budgets and estimates |
| `OPS` | Platform job operations: proposals, outcomes and reconciliation |
| `ENV` | Readiness checks without a more specific code |

Numbers `001` to `899` are assigned in sequence within an area and never reused; `900` to `999` are generic fallbacks.
A gap is a retired code.
Warning or failure status is reported separately, so the number does not imply urgency.

## Project Storage

### `HW-PROJECT-001`: Project Storage Is Unavailable

Enter an existing writable directory dedicated to the analysis.
Check the path and permissions:

```bash
pwd
ls -ld .
```

On Terra, use a child directory below `/home/jupyter`.
On Carina, use an approved writable project directory below `/projects`.

### `HW-PROJECT-002`: Project Setup Needs Attention

Run `heartwood` from the intended project and confirm **Use this project**.
If local `.heartwood/` already exists but has no valid state marker, preserve it and investigate its contents before any migration or replacement.
For a managed deployment, have the operator verify the manifest, project UUID and external state path; do not delete markers or fall back to project-local state.

### `HW-PROJECT-003`: Choose a Dedicated Project Directory

Heartwood refuses a filesystem root or home directory because that boundary is too broad.
Create and enter a child folder, then run `heartwood doctor` again.

### `HW-PROJECT-004`: Project State Migration Is Incomplete

`heartwood gateway serve --deployment` found project-local state that has not been migrated, a migration that is still staging or publishing, or a published migration without its completion receipt.
A gateway started while `heartwood project migrate` runs is refused this way and creates nothing.
Wait for the migration command to finish and start the gateway again.
If the migration was interrupted, run `heartwood project recover-migration --deployment` with the same manifest; do not delete the fence, the journal or either copy of the state.

## Configuration

### `HW-SETUP-001`: Project Configuration Needs Attention

Run `heartwood setup` or open browser settings and choose a model connection.
Do not repair `config.toml` by inserting a token or copying a profile from another platform.

Under a deployment manifest, the same code reports stored settings the manifest's policy cannot narrow, such as a project destination list that shares no destination with the manifest's.
`heartwood doctor --deployment` reports it, and `heartwood gateway serve --deployment` stops with it before serving the browser.

### `HW-SETUP-002`: Model and Policy Settings Do Not Agree

Open setup and select a model connection the policy permits.
This regenerates the non-secret model profile and matching platform policy as one update.

When the summary names the deployment or platform policy, the policy that applies before each model call refuses the selected route, for example because its declared data destination is not in `allowed_data_destinations`, or its endpoint, capability tier or credential is not permitted.
Readiness and selection apply the same effective policy as each model call, including the deployment manifest's ceiling, so selecting the same connection again is refused.
Select a route the policy permits, or ask the operator, who alone can change a deployment's policy.

### `HW-SETUP-003`: Stored Review Mode Is Not Permitted by the Deployment

The deployment manifest's `allowed_action_review_modes` no longer lists the project's stored review mode, so Heartwood applies Review Every Action, which every policy permits.
Each session records the change before its next model dispatch.
Select any mode the deployment permits in `/permissions`, browser settings or with `--review-mode`; the selection replaces the stored mode and ends the warning.

### `HW-SETUP-004`: Capability Is Not Offered on This Platform

The project's stored model connection, or a requested one, is not offered by the detected platform.
Setup lists only the connections the platform offers, so this happens when a project moves to another platform or a client names a connection directly.
`heartwood doctor` and the browser readiness view report a stored connection, and the terminal, browser and notebook refuse a requested one with this code.
Run `heartwood setup` or open browser settings and select a connection this platform offers.

## Models and Credentials

### `HW-MODEL-001`: No Model Is Selected

Run `heartwood` and choose a model returned by an available connection.
For Heartwood-managed inference, download or import the model before starting the agent.

### `HW-CREDENTIAL-001`: Model Credential Is Unavailable

Reopen setup and enter the provider API key, or ask the platform operator how its secret binding is supplied.
An operating-system keyring entry is project-scoped, so another project may correctly ask again.
For **Sign in with ChatGPT**, repeat the one-time-code flow.
If the account should no longer be used, run `heartwood models forget openai-subscription` before signing in with another account.

### `HW-CREDENTIAL-002`: Model Credential Isolation Is Unavailable

The selected model uses a credential, but the active platform provides application scrubbing rather than a separate model-only credential boundary.
Outside an isolated worker the session then reviews every action, and **Review Changes** is listed as unavailable with this reason.
Choose a credential-free or platform-isolated model route to select it.

Application scrubbing still keeps the credential out of supported tool inputs, project state, browser and notebook output, session events, logs, and audit exports.
It does not isolate same-identity process memory or every operating-system resource.
See [Security and Controlled Data](../operate/security.md#model-credential-isolation).

### `HW-MODEL-002`: Heartwood-Managed Model Files Are Unavailable

Run:

```bash
heartwood models managed
heartwood models inspect OWNER/MODEL
```

Choose a Qualified configuration, confirm enough free disk, and download again.
For a Heartwood bundle, run `heartwood models inspect-bundle PATH` and then `heartwood models import PATH`.
For raw GGUF or [safetensors](glossary.md) files, use `heartwood models import` with the immutable source revision and license record.

### A Model Bundle Cannot Be Imported

Run `heartwood models inspect-bundle PATH` from the destination project and keep the complete error message.
Do not edit the archive or its manifest.

For a checksum, size, or file-list mismatch, obtain a new bundle from the connected source project.
For an interrupted import, rerun the unchanged command; Heartwood discards incomplete private staging before retrying.
For an existing model mismatch, investigate changes to `models/` under the bound state directory before attempting recovery.
For a runtime warning, install a compatible Heartwood release or choose a model supported by the destination compute.

## Agent Runtime

### `HW-AGENT-001`: Agent Runtime Is Unavailable

The installed OpenHands dependency set cannot be loaded.
Run `heartwood --version`, reinstall the same Heartwood release through its documented installation route, and rerun `heartwood doctor`.
Model download, import, and inspection commands remain available so a broken agent runtime does not block project recovery.

### `HW-AGENT-002`: An Agent Action Failed

Heartwood received a failed action from the OpenHands runtime.
Review the proposed action set and model connection in Activity & audit, then try the task again.
Heartwood deliberately omits provider and project content from this diagnostic.

### `HW-AGENT-003`: The Agent Conversation Stopped

OpenHands stopped the conversation before the task completed.
Review Activity & audit, verify the model connection, and start the task again.

### `HW-AGENT-004`: The Agent Worker Stopped

The background OpenHands worker stopped unexpectedly.
Run `heartwood doctor`, review Activity & audit, and try the task again.

### `HW-AGENT-005`: The Agent Cannot Perform That Operation

The requested operation does not match the conversation's current state.
Review the active task or pending action set before trying the operation again.

### `HW-AGENT-006`: An Approved Action Has an Unknown Outcome

Heartwood stopped, or the call that handed an approved action to OpenHands failed, before OpenHands recorded whether the action completed.
The same applies when a restarted conversation shows an action that was already approved as waiting for review again.
Do not approve or repeat the action blindly.
Inspect the project files and Activity & audit, then continue in a new session once you understand the project state.

### `HW-AGENT-007`: An Agent Turn Has an Unknown Outcome

Heartwood stopped during a model turn before OpenHands recorded a stable completion boundary.
Do not repeat the task blindly because the provider may already have processed the request.
Inspect the session replay and Activity & audit, then continue in a new session.

### `HW-AGENT-008`: Model Provider Authentication Failed

The model provider rejected the credential configured for the active connection, during a task or while listing models.
For a platform connection, the deployment supplies the credential; ask the operator to renew it.
Otherwise open model settings, update the credential through the supported secret mechanism, and validate the connection before retrying.

### `HW-AGENT-009`: Model Provider Quota Exhausted

The provider reported that the configured quota, credit, or budget is exhausted.
For a platform connection, the deployment sets the budget; ask the operator to raise it.
Review the approved provider account or select another approved model connection before continuing.

### `HW-AGENT-010`: Model Provider Rate Limited

The provider temporarily limited requests.
Wait briefly and retry the task without repeating any action whose outcome is unknown.

### `HW-AGENT-011`: Model Configuration Is Invalid

The selected model, endpoint, or provider configuration cannot serve the requested task.
A task reports it when the provider no longer allows the connection's credential to use the selected model.
Model setup reports the same code when the model is no longer in the connection's model list or the deployment declares no protocol Heartwood supports for it.
A withdrawn model stays selected until you choose another; Heartwood does not replace it.
Open model settings, choose or correct the connection, and validate it before retrying.

### `HW-AGENT-012`: Model Provider Is Unavailable

The configured model service could not be reached or returned a temporary availability failure, during a task or while listing models.
Check the connection and provider status, then retry the task.

### `HW-AGENT-013`: Session Runtime Is Incompatible

The session was saved by a different OpenHands SDK version and cannot be resumed without a supported migration.
Keep its history and start a new session in the same project to continue working with the updated runtime.
To resume the original conversation, use the Heartwood version that created it.
Do not edit or delete the persistence marker to bypass this check; pending actions must not be replayed against an unqualified runtime.

### `HW-AGENT-999`: The Agent Runtime Reported an Error

An execution backend returned an error without a more specific stable code.
Review Activity & audit, run `heartwood doctor`, and try the task again.

## Project Inspection

### `HW-WORKSPACE-001`: Project Path Is Invalid

Use a normalized path relative to the current project.
Do not use an absolute path, `..`, backslashes, repeated separators, control characters, or a trailing separator.

### `HW-WORKSPACE-002`: Private Project State Is Not Available

The Files and Changes interfaces never expose `.heartwood/` or `.git/`, including nested directories with either name.
Use the documented audit export or Git commands from a trusted terminal when you are authorized to inspect that state.

### `HW-WORKSPACE-003`: Symbolic Link Inspection Is Not Available

Heartwood does not follow symbolic links through its read-only project API.
Inspect the intended regular file through its project-relative path.

### `HW-WORKSPACE-004`: Expected a Directory

Use `heartwood files show FILE` for a file or choose a directory for `heartwood files list`.

### `HW-WORKSPACE-005`: Project Entry Is Unavailable

The path does not exist or its metadata cannot be read.
Confirm the spelling and project permissions, then retry from the same project directory.

### `HW-WORKSPACE-006`: Requested Tree Depth Is Unsupported

Choose a positive depth no greater than the limit reported by the workspace response.
Omit `--depth` to use the default bounded depth.

### `HW-WORKSPACE-007`: File Fingerprint Is Unavailable

Use a regular project file, not a symbolic link, and wait for any process writing it to finish.
The file must fit within the capture operation's byte limit.
Retry capture before relying on its digest as execution evidence.

## Skills

### `HW-SKILL-001`: Installed Skill Storage Needs Attention

Each installed Skill is stored alone in its content-addressed directory under the project's Skill store.
Something else in that directory, such as a directory written beside the Skill or a staging directory left by an interrupted installation, has no verified digest or activation record.
Heartwood never loads it, and the verified Skill stays active.
Remove each path named in the check summary, or under **Unverified content** in the Skill list, with an account that owns the Skill store.
The same check fails when the installation index cannot be read or locked, because no conversation can select Skills until the project state is repaired or restored.

### `HW-SKILL-002`: Bundled Skills Do Not Match This Release

Heartwood pins the complete-tree digest of every Skill bundled with the release.
A failing check names a bundled Skill that is missing or has changed; Skills and specialists stay unavailable until it is restored.
In a source checkout, run `git submodule update --init` and discard local changes under `vendor/heartwood-skills`.
Restoring tracked files does not remove git-ignored files inside a Skill folder, such as `__pycache__/` or `.DS_Store`, and any extra file changes the Skill's digest; list them with `git -C vendor/heartwood-skills status --ignored skills` and delete them.
In a native installation, reinstall the same release; in a container, use the published image unchanged.
A warning names entries in the bundled Skill directory that are not part of the release, such as `.DS_Store`; Heartwood never loads them, and you can delete them.

## Research Services

Research MCP servers are managed in the browser's Research services panel or with `heartwood services`.
[Research MCP Servers](../operate/platform-contract.md#research-mcp-servers) describes how calls are reviewed.

### `HW-MCP-001`: Research Service Destination Is Not Allowed

The server URL is not at an origin listed in the deployment manifest's `mcp_destinations`, or it resolves to a private, link-local or metadata address.
Without a deployment manifest, or with no listed destinations, every research service is refused.
Plain HTTP is accepted only on loopback.
A private address, such as a cluster service, is accepted only when the operator sets `private_address` for that destination; link-local and metadata addresses never are.
Ask the operator to add the exact origin, or use a server at an approved origin.

### `HW-MCP-002`: Research Service Credential Is Expired

The token held for the server has an expiry time in the past, so the gateway does not send it.
Enter a current token when you test the server again.
Heartwood reads the expiry from the token; the service still verifies the token itself.

### `HW-MCP-003`: Research Service Tool Changed After Review

A tool's definition differs from the definition you reviewed.
A test that lists a changed definition removes the tool from the enabled set, a pending call to it is not sent, and a selection with an outdated digest is refused.
Test the server, compare the listed tools, and select them again.

### `HW-MCP-004`: Research Services Require an Isolated Worker

Research service calls run only in sessions with an isolated worker, because each call needs outside review.
The planning projection reports the agent planning tools as unavailable with this code, because their calls need the same review.
Start the gateway with a deployment manifest that configures an isolated worker.
Research services also need the manifest to list MCP destinations; planning tools need only the worker.
`heartwood services` can still manage the servers of such a deployment.

### `HW-MCP-005`: Research Service Is Unavailable or Refused the Request

The server did not answer, refused the credential, redirected to another origin, or did not list its tools.
Heartwood follows redirects only within the same origin.
The MCP client reports a refused credential and an unavailable service the same way, so check both the URL and the token.

### `HW-MCP-006`: Research Service Tool Cannot Be Selected

The deployment does not list the tool for the server's origin, or the tool does not accept a `project_id` and an `operation_id`.
Heartwood supplies both values so the service can scope the call and recognize a repeated operation.
Tools without them cannot be selected.

### `HW-MCP-007`: Planning Tools Are Not Offered With Another Outside Service

The gateway runs in an embedding that supplies its own outside service, so worker sessions receive no agent planning tools.
The planning projection reports them as unavailable with this code.
No deployment manifest change enables them.
Researchers can still use planning through `/plan`, the browser or the notebook.

## Deployment Manifests

### `HW-DEPLOY-001`: Deployment Manifest Is Unavailable or Invalid

The manifest passed with `--deployment` is missing, unreadable, not operator-owned and write-protected, outside the expected schema, or does not bind this research directory.
Heartwood stops before creating any state and never falls back to project-local state.
Have the operator correct the manifest and its file permissions, then start the gateway again.

### `HW-DEPLOY-002`: Deployment Policy Is Wider Than the Platform Policy

The manifest's `[policy]` table permits something the detected platform adapter does not: a review mode, capability tier, credential backend, credential name, model endpoint, a lower aggregate count floor, or a checkpoint signer the policy itself excludes.
The message names the field.
A manifest can only narrow the platform policy; remove the extra entry or run on a platform whose adapter permits it.
`heartwood doctor --deployment` reports the policy that applies once the manifest is accepted.

### `HW-DEPLOY-003`: Deployment Requirement Is Not Met

The manifest's `[requirements]` table requires an isolated worker or required-mode checkpoint evidence, and the manifest does not configure it.
Add the qualified `[worker]` or `[evidence]` section, or remove the requirement only after the operator decides the deployment no longer needs it.

### `HW-DEPLOY-004`: Deployment Manifest Changed While Heartwood Was Running

The gateway rereads the manifest before each command that can continue agent work.
A change that only narrows the `[policy]` table applies immediately and is recorded in each session before its next model dispatch.
A narrower policy that excludes the project's stored review mode applies Review Every Action, as [`HW-SETUP-003`](#hw-setup-003-stored-review-mode-is-not-permitted-by-the-deployment) describes.
Any other change, including a wider policy or a changed worker, evidence or project binding, stops further dispatch until the gateway restarts.
Pausing and rejecting remain available.
Restart the gateway to apply the change under the operator's authority; restoring the earlier manifest does not resume dispatch in the running gateway.

## Managed Workers

### `HW-WORKER-001`: Sandbox Gateway Lacks a Required Deletion Capability

`heartwood gateway serve --deployment` read the registered sandbox gateway's info, and it did not report exactly one compute driver of the implementation the manifest's `driver` selects with the three required [capabilities](../operate/platform-requirements.md#sandbox-gateway), or the info could not be read.
The message names the reported gateway version and each missing capability when the gateway answered.
Confirm that the gateway named in the manifest is running and reachable with its registered mutual TLS identity.
An upstream gateway release that does not provide these behaviors is refused; register the [sandbox gateway build](../operate/platform-requirements.md#sandbox-gateway) that reports them.
The same condition after start stops the affected worker launch or reconciliation before any sandbox, volume or deletion request.
A refused launch shows `HW-WORKER-001` in the session and needs no reconciliation: nothing was created, and the attempt is recorded as revoked together with what the gateway reported.
The session cannot start another worker in the same gateway process; correct the gateway and restart `heartwood gateway serve`, which then replaces the attempt.
A refused reconciliation leaves the attempt fenced until a later start-up succeeds.

### Managed Worker Control Is Unavailable

`gateway serve` prints `Managed worker control is unavailable:` with the reason when the native tools, gateway registration, or protected state named by `[worker]` cannot be used.
`deployment manifest is unavailable or invalid` and the other `deployment …` messages name the [manifest requirement](configuration.md#deployment-manifest) that failed, such as ownership, write protection, a symbolic link, a path inside the project, or a changed policy file digest.
Correct the operator files; do not loosen their permissions or move them into the project.

### `HW-WORKER-002`: Worker Attempt Ran on Other Sandbox Infrastructure

`heartwood gateway serve --deployment` found a session whose worker attempt was left unrevoked, but the attempt ran on a different sandbox control plane or container runtime than the deployment manifest names now, or its record predates the recording of that infrastructure.
Heartwood leaves the attempt fenced and does not record revocation, because a worker missing from the current infrastructure may still be running on the earlier one.
Stop the worker on the infrastructure it ran on, using that control plane's own tools, and continue the work in a new session.
Pointing the manifest back at the original control plane and Docker daemon, or cluster and namespace, lets the next gateway start reconcile the attempt normally.
With the Kubernetes driver, a changed API server address or certificate authority in `kubeconfig`, a changed `workspace`, or a namespace deleted and created again, also counts as other infrastructure.

### `HW-WORKER-003`: Worker Revocation Was Not Observed

The gateway could not observe that a worker attempt an earlier gateway process left running was stopped, typically because the sandbox control plane or the Docker daemon was unavailable.
The attempt remains fenced, and the session refuses commands until revocation is observed; it stays readable.
The gateway retries while it runs, with a growing interval of up to 10 minutes, so restoring the control plane is enough; restarting the gateway retries at once.
A worker whose revocation fails while the gateway runs, including when its session's service is being closed, is handled the same way: the session stays fenced, refuses commands as retryable and stays readable until the retry observes revocation.
When the gateway's request to create the worker failed or went unanswered, revocation asks the sandbox gateway to confirm that the worker's name is absent, which also makes the gateway reject a later creation under that name; the attempt is then recorded as never created.
A request the control plane rejected as invalid, unauthorized or unauthenticated is recorded as never created by the same gateway process without that question.
Until the sandbox gateway answers, the attempt stays fenced, and the retry deletes the worker if it appears.
A gateway process that ended after it prepared a worker and before it recorded the worker's runtime or its revocation leaves the attempt fenced the same way.
Continue such a session's work in a new session.
Do not delete the session's records or writer files to release it.

### A Replacement Worker Started

A session whose earlier worker was revoked shows "A new worker replaced the earlier one" when its next task starts a replacement.
The replacement does not remember the earlier conversation and starts with an empty sandbox workspace; restate the context it needs.
The earlier worker's unfinished tasks are shown as superseded, and its model usage is listed as "Earlier Workers" and included in the session total.
A session is refused instead while its earlier worker left an unsettled run, an unended pause, an action awaiting review, an unrecoverable outcome, or a non-terminal outside operation or result release; the session shows "New Worker Unavailable" with the reason.
Inspect its record and continue in a new session.

## Action Review Mode

`--review-mode`, `/permissions`, browser settings, and `PUT /settings/actions/review` refuse a mode for one of these reasons, shown with the mode:

| Cause | Resolution |
|---|---|
| The mode is outside the deployment's `allowed_action_review_modes` | Only the operator can change the ceiling; in a local launch the built-in policy lists only `review-all` and `review-changes`, so `review-outside-effects` is reported as not permitted by the deployment's policy |
| `review-outside-effects` without an admitted sandbox worker | Available only under a deployment manifest with a `[worker]` table; a local launch never runs one, so the mode is always unavailable there |
| Reduced review outside a sandbox worker with a credential-backed model route | See [`HW-CREDENTIAL-002`](#hw-credential-002-model-credential-isolation-is-unavailable) |
| A session is active and the selection asks less often than the current mode | Finish or resolve the active session work; a stricter mode is always accepted |

## Research Planning

These conditions appear in the planning overview of the terminal, browser and notebook.
The platform owns budgets, prices and admission; see [Launch Context, Budgets and Estimates](../operate/platform-contract.md#launch-context-budgets-and-estimates).

### Earlier Planning Evidence References

A journal containing a version 1 prediction with a free-text evidence reference such as `sample.csv` cannot be opened by the current planning reader.
The gateway identifies the earlier format and leaves the journal in place.
Because the protected project journal can hold several launch contexts, starting a fresh journal affects planning history for all of them.
Stop all gateways using that protected project state and archive `planning.jsonl` and any `.planning.jsonl.pending` file under the same access controls.
Verify the archived bytes. If an append is pending, complete recovery with the matching reader and archive the recovered journal as well.
With all writers stopped, move the recovered journal to a distinct, unused backup name.
Restart the gateway with the original journal path vacant to start fresh planning state.
To inspect the historical evidence, use the matching earlier reader on an isolated copy of the archive.

### `HW-PLAN-001`: Platform Planning Information Is Unavailable

The platform's planning service could not be reached, did not accept the gateway's credential, or returned a response that did not match the contract or the launch context.
Plans, estimates and budget observations recorded earlier stay visible with their observation times.
Retry later; when the condition persists, ask the platform administrator to check the planning service and the credential the deployment issued for it.

### `HW-PLAN-002`: The Platform Refused the Planning Request

The planning service answered, and refused the request.
Typical causes are a platform project the gateway's identity may not use, or a prediction the service does not accept.
Heartwood does not show the service's text.
Confirm the project's platform access with the platform administrator.

### `HW-PLAN-003`: The Platform Estimate Has Expired

The latest estimate for these plan elements is past its validity time.
An expired estimate stays in the plan's history.
Request a new estimate before approving work; the platform decides admission with its current prices and budgets either way.

### `HW-PLAN-004`: A Platform Budget Changed After the Estimate

A budget observation read after the estimate differs from the one read before it, for example because other work spent from the same allocation or an administrator changed it.
Compare the plan with the current budgets and revise it if needed.
A reported balance never guarantees that the platform admits work.

### `HW-PLAN-005`: The Platform Estimate Is Incomplete

The estimate's coverage is partial or unknown, for example because the platform does not estimate one kind of work or has no price for a runtime profile.
The elements without a complete cost show their limitations; their cost stays unknown.
Review them before approving work, or revise the plan to a configuration the platform estimates.

### `HW-PLAN-006`: The Platform Denied an Operation

The platform denied or revoked authorization for a reviewed proposal, or denied an operation, even if the researcher approved it.
Revise the plan, or request more allocation through the platform's own process.
Heartwood never resubmits a denied operation automatically.

## Platform Job Operations

These conditions appear with a platform job in the session's agent actions and in its outside operation review in the terminal, browser and notebook.
The platform runs and authorizes each job; see [Platform Job Operations](../operate/platform-contract.md#platform-job-operations).
A job the platform denies, even after you approved it, shows [HW-PLAN-006](#hw-plan-006-the-platform-denied-an-operation).

### `HW-OPS-001`: The Platform Did Not Describe the Operation

The job service's proposal tool did not answer, refused the request, or returned a proposal for another service or tool.
Without the platform's description of the limits, data scope and destination, the review cannot be approved.
Reject the review, or let it expire; a new review of the same call asks the platform again.

### `HW-OPS-002`: A Platform Operation Failed

The platform reported the job as failed, either after it ran, for example because its executor reported an error, or because the platform never received it.
The operation shows the platform's report and its platform record.
Read the platform record, revise the job, and let the agent propose it again; Heartwood never resubmits a job automatically.

### `HW-OPS-003`: A Platform Operation Has an Unknown Outcome

The reply to a submission or cancellation was lost or did not arrive within the destination's time limit, so the platform may or may not have received it.
Heartwood asks the service's status tool about the same operation identifier until the platform reports it, and proposes or sends nothing else to that service meanwhile.
An operation the platform never received is reported as failed.
When the condition persists, check that the job service is reachable and that the token for it is valid.
While the outcome stays unknown, no other job on that service can be submitted or cancelled from this session; use the platform's own tools for running jobs meanwhile.
Operations from a session whose platform launch context has since changed cannot be reconciled from a new launch context.
That service then stays blocked for the rest of the session; start a new session to submit jobs to it again.

## Managed Compute

### `HW-COMPUTE-001`: A Compute Allocation May Be Required

On Carina, start `heartwood`, inspect the full Slurm request, and approve it only when the resources and duration are appropriate.
Use `heartwood runtime start --dry-run` to inspect without allocating.

### `HW-COMPUTE-002`: Allocation Scratch Storage Is Unavailable

Request an allocation that provides writable job scratch or use the platform's supported runtime path.
Keep durable project and model state in approved project storage.

### `HW-COMPUTE-003`: A Compatible GPU Is Unavailable

Choose a hosted model, a GGUF CPU model, or GPU-enabled compute.
For containers, verify the NVIDIA Container Toolkit and `--gpus all`; for Terra, select the GPU image and attach an NVIDIA GPU.
The CUDA 12.9 runtime requires compute capability 7.5 or newer, so P4, P100, and V100 GPUs are rejected before model startup.
Compare the detected environment with [GPU Compatibility](gpu-compatibility.md).

### Managed Inference Outcome Is Unresolved

Heartwood reports `managed inference outcome is unresolved` when an earlier Heartwood-managed launch did not observe its inference process exit.
The launcher stops the process and clears its record on normal exit, `Ctrl+C`, `SIGTERM`, and `SIGHUP`.
A launcher that was killed outright, lost its host, or could not start or stop the process leaves `runtime/managed-inference.pending.json` in the project state directory, `.heartwood/` for a local project.
New managed launches, project migration, and migration recovery stay blocked while that record exists, because an absent launcher does not prove that inference stopped.

1. Read the record for the launcher process identifier and research directory, and confirm on the host or allocation that started it that the launcher is no longer running.
2. Confirm with the platform supervisor, for example `squeue` and `nvidia-smi` or the process list, that no vLLM or llama.cpp process started for this project remains, and stop any that does.
3. Remove `runtime/managed-inference.pending.json` and start Heartwood again.

Do not remove the record while the process outcome is unknown; a surviving runtime can keep reading model files that a migration or a second launch is about to change.

## Terra

### `HW-TERRA-001`: Choose a Dedicated Terra Project Directory

```bash
mkdir -p /home/jupyter/heartwood-project
cd /home/jupyter/heartwood-project
heartwood doctor
```

Do not use `/home/jupyter` itself as the agent boundary.

### `HW-TERRA-002`: Terra GPU Support Is Unavailable

Use the `-terra-gpu-nvidia` image and attach supported GPU compute, or choose hosted inference.
Delete and recreate the Cloud Environment with a T4 while retaining the persistent disk; Terra does not apply a changed image or GPU selection to an existing environment.

## Environment Fallback

### `HW-ENV-999`: Environment Check Needs Attention

Heartwood encountered a readiness check that has no more specific public diagnostic.
Run `heartwood doctor`, inspect the failed check and its next action, and include the structured `heartwood doctor --json` output in a synthetic issue report if the condition persists.

## Gateway Ingress

### `HW-INGRESS-001`: Gateway Ingress Configuration Is Unsafe

The requested bind, origin, prefix, or proxy trust values do not form one safe route.
Use the default loopback gateway for direct access.
For a Jupyter or trusted platform proxy, configure the exact browser-visible origin and base path, the expected prefix behavior, and the trusted proxy source boundary.
`identity-proxy` ingress also needs a non-loopback bind and the manifest's `[identity]` table, and the table is refused with every other mode.

Do not solve this diagnostic by widening the bind or using a wildcard origin.
See [Add a Platform](../operate/platform-integration.md#gateway-ingress).

### `HW-INGRESS-002`: Gateway Request Does Not Match the Configured Route

Use the exact URL supplied by the deployment.
If the route is operator-managed, verify the upstream source address, external host and protocol, stripped or preserved prefix, HTTP and WebSocket origins, and complete forwarded-header set.

The proxy must remove forwarding and identity headers supplied by the client before adding its own values.
Do not disable host, origin, or path validation to make the request pass.

### `HW-INGRESS-003`: Gateway Request Is Missing the Launch Capability

The request reached the gateway route but did not carry the capability that `heartwood gateway serve` issued at startup.
Open the launch link printed by the terminal that started Heartwood; it works once and authorizes that browser tab.
Reload the same tab to retain access; after restarting the gateway, use its new launch link.
If the link was already used or the gateway was restarted, stop Heartwood, start it again, and open the new link.

Automation must send the capability supplied through `--capability-fd` in the `X-Heartwood-Capability` header.
Do not share the link or the secret with other users or processes.

### `HW-INGRESS-004`: Gateway Request Carries No Accepted Identity

The gateway runs with `identity-proxy` ingress and the request carried neither a live sign-in nor an accepted access token, or a sign-in was refused.
Open the gateway's URL in the browser and sign in as the researcher the gateway was started for.
A sign-in ends with its tokens and at every gateway restart; reload the page to sign in again.

A sign-in is refused when the account is another person's, when the sign-in attempt is older than ten minutes or was already completed, or when the provider rejects the gateway's client secret or redirect URI.
It is also refused when the callback arrives in another browser than the one that started the sign-in, when the browser blocks the gateway's cookies, or when the gateway restarted in between.
An access token is refused unless the configured issuer signed it, it is unexpired, its `aud` contains the gateway's client and its `sub` is the bound researcher.
An access token signed with an algorithm other than RS256 or ES256, or without a key identifier, and an identity token are refused as well.
Operators compare the manifest's [`[identity]` table](configuration.md#identity) with the provider's client: issuer, client identifier, secret, subject and the redirect URI `<public origin><base path>/oauth_callback`.

A research service that takes an exchanged token reports this code while no sign-in is live, and when the provider refuses a token for that service.
Test and add such a service in the signed-in browser; `heartwood services` has no sign-in and reports this code for every such service.
When this code refuses an approval of one of the service's operations, the terminal, browser and notebook show it and the review stays open.
Sign in at the gateway again and approve the operation again.
A review is valid for five minutes.
While the service refuses with this code, an expired review of a submission is not replaced and no review opens for a new submission to the service.
The terminal, browser and notebook show **A research service call cannot be reviewed yet** with this code once for each such call, and the agent continues without the call's result.
To describe calls that have no review, the gateway asks the provider at most every ten seconds for each service while the sign-in is unchanged; a sign-in after none was live or a connection to the service, such as an approved operation, ends that wait.
The first session read after the next sign-in opens a review with the platform's description, and the new review needs its own approval.
Operators compare the destination's `token_exchange` scope and audience with the scopes the provider grants the gateway's client.

### `HW-INGRESS-005`: Identity Provider Is Unavailable

The gateway could not reach the identity provider, or the provider answered with a document the gateway cannot use.
Status `503` means the request to the provider failed or timed out; status `502` means the provider answered with an error or an unusable document.
Retry when the provider is reachable.
A research service that takes an exchanged token reports this code when the gateway cannot obtain its token, and an approval of one of its operations is refused with it in the terminal, browser and notebook.
The review stays open; approve it again once the provider is reachable.
While the provider stays unavailable, an expired review of a submission is not replaced and no review opens for a new submission to the service.
The terminal, browser and notebook show **A research service call cannot be reviewed yet** with this code once for each such call, and the agent continues without the call's result.
To describe calls that have no review, the gateway asks the provider at most every ten seconds for each service while the sign-in is unchanged.
The first session read after the provider is reachable again and that interval has passed opens a review with the platform's description, and the new review needs its own approval.

Operators check that `provider_url` is reachable from the gateway, that `ca_file` holds the authority of the provider's certificate, and that the discovery document's `issuer` equals the configured `issuer`.
The discovery document's authorization, token and key endpoints must lie on the origin of `issuer` or of `provider_url`.

## Browser Access

If the browser page does not open:

1. Keep the launching terminal running.
2. Confirm that `heartwood --interface web` reported ready.
3. Use the printed launch link; when it was already used, start Heartwood again.
4. Check whether port `8767` is in use, or start with another `--port`.
5. Run `heartwood doctor` from the same directory.

The browser interface is not supported on Terra or Stanford Carina.
Use the terminal or the Terra notebook interface instead of constructing a proxy path.

Do not work around a proxy failure by binding the gateway publicly without authentication.

## Session Ownership and Interrupted Commands

If Heartwood reports that a session is active in another process, stop the terminal, browser server, or notebook kernel that owns it and try again.
Do not remove `.writer.lock` or `.writer.json`; the operating-system lock determines ownership, and Heartwood reclaims stale metadata after the prior process has exited.
If Heartwood reports that state storage does not support required process locks, preserve the research files and matching state before migrating to a native local, attached persistent or qualified filesystem.
A managed migration must also preserve the project identity and update the operator binding explicitly.
Do not substitute a state directory from another project or use an object-store mount as session storage.

A completed request can be retried with the same command identifier and content without repeating model or tool work.
If Heartwood reports that a command was interrupted after acceptance, replay the session and verify the project files and audit records before continuing.
Then continue in a new session; the interrupted session remains read-only so an uncertain action cannot be repeated.
Do not edit or remove files below the session's `.commands/` directory or its recovery journal.

## Audit Verification and Checkpoints

Run the full session check before transferring evidence:

```bash
heartwood --session-id session-main audit verify
```

If verification fails, stop mutating that session and preserve the complete project state for authorized review.
Do not edit an audit line, remove a recovery journal, or recreate a missing hash.

A checkpoint output, signer registry, trusted public key, signer credential, and local fallback key must be outside the Heartwood project.
Run `heartwood audit signer list` to confirm that a deployment registry is available and that the expected profile is active.
For the explicit offline fallback, keep `heartwood signer serve-local` running and confirm that its Unix socket endpoint, profile, key fingerprint, and owner-only token still agree.
The socket and its immediate directory must be owner-only, owned by the current user, and outside the project; directory symlinks are rejected.
If startup reports an existing socket, confirm that no signer is using it before removing a stale socket and restarting the service.
Use a new output directory, a valid retention date on or after checkpoint creation, and storage that supports native process-shared locks.
Verify a restored bundle with the active deployment profile or an independently retained public key before relying on it.

See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md) for the complete workflow and the limits of the signed retention declaration.

## Collect Safe Diagnostics

Share the Heartwood version, `heartwood doctor --json`, platform, image tag or digest, failing `HW-*` code, and a minimal synthetic reproduction.
Remove project content, credentials, protected data, signed URLs, user identifiers, and unrestricted logs before attaching anything to a public issue.
