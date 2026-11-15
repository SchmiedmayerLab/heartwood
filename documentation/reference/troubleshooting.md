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

## How Diagnostic Codes Work

Codes use `HW-{AREA}-{NNN}`.
`AREA` identifies where the condition must be resolved; it does not identify severity.

| Area | Conditions |
|---|---|
| `PROJECT` | Project boundary, storage, and private project state |
| `SETUP` | Non-secret Heartwood configuration and policy agreement |
| `MODEL` | Model selection, compatibility, and managed model files |
| `CREDENTIAL` | Provider API key, subscription credential, or managed-identity availability |
| `INGRESS` | Gateway binding, proxy trust, origins, forwarding metadata, and paths |
| `AGENT` | OpenHands agent runtime availability |
| `WORKSPACE` | Read-only project file and change inspection |
| `COMPUTE` | Scheduler allocation, GPU, memory, and scratch storage |
| `TERRA` | Terra-specific project and compute requirements |
| `WORKER` | Managed worker sandbox gateway |
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

## Configuration

### `HW-SETUP-001`: Project Configuration Needs Attention

Run `heartwood setup` or open browser settings and choose a model connection.
Do not repair `config.toml` by inserting a token or copying a profile from another platform.

### `HW-SETUP-002`: Model and Policy Settings Do Not Agree

Open setup and select the intended model connection again.
This regenerates the non-secret model profile and matching platform policy as one update.

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

Heartwood stopped after an action was approved but before OpenHands recorded whether it completed.
Do not approve or repeat the action blindly.
Inspect the project files and Activity & audit, then continue in a new session once you understand the project state.

### `HW-AGENT-007`: An Agent Turn Has an Unknown Outcome

Heartwood stopped during a model turn before OpenHands recorded a stable completion boundary.
Do not repeat the task blindly because the provider may already have processed the request.
Inspect the session replay and Activity & audit, then continue in a new session.

### `HW-AGENT-008`: Model Provider Authentication Failed

The model provider rejected the credential configured for the active connection.
Open model settings, update the credential through the supported secret mechanism, and validate the connection before retrying.

### `HW-AGENT-009`: Model Provider Quota Exhausted

The provider reported that the configured quota, credit, or budget is exhausted.
Review the approved provider account or select another approved model connection before continuing.

### `HW-AGENT-010`: Model Provider Rate Limited

The provider temporarily limited requests.
Wait briefly and retry the task without repeating any action whose outcome is unknown.

### `HW-AGENT-011`: Model Configuration Is Invalid

The selected model, endpoint, or provider configuration cannot serve the requested task.
Open model settings, correct the connection, and validate it before retrying.

### `HW-AGENT-012`: Model Provider Is Unavailable

The configured model service could not be reached or returned a temporary availability failure.
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

## Managed Workers

### `HW-WORKER-001`: Sandbox Gateway Version Is Not Declared as Qualified

`heartwood gateway serve --deployment` could not read the version the registered sandbox gateway reports, or that version is not in the manifest's `gateway_versions`.
Confirm that the gateway named in the manifest is running, reachable with its registered mutual TLS identity, and the one your qualification evidence covers.
After an upgrade or replacement, obtain evidence for the [behavior native qualification depends on](../operate/platform-requirements.md#gateway-behavior-behind-native-qualification) before adding the reported version.
Never add a version only to pass the check: it compares your declaration with the gateway's own report and proves nothing about its behavior.
The same condition after start stops that worker launch or reconciliation and leaves the attempt fenced.

### Managed Worker Control Is Unavailable

`gateway serve` prints `Managed worker control is unavailable:` with the reason when the native tools, gateway registration, or protected state named by `[worker]` cannot be used.
`deployment manifest is unavailable or invalid` and the other `deployment …` messages name the [manifest requirement](configuration.md#deployment-manifest) that failed, such as ownership, write protection, a symbolic link, a path inside the project, or a changed policy file digest.
Correct the operator files; do not loosen their permissions or move them into the project.

### Worker Revocation Was Not Observed

At start, `gateway serve` reports `Worker revocation was not observed for session ID; its attempt remains fenced until the next gateway start.`
The earlier worker may still be running.
Confirm through OpenShell and Docker that its sandbox is gone or stop it there, then restart the gateway so reconciliation records the revocation.
Do not delete the session's records or writer files to release it.

### A Replacement Worker Started

A session whose earlier worker was revoked shows "A new worker replaced the earlier one" when its next task starts a replacement.
The replacement does not remember the earlier conversation and starts with an empty sandbox workspace; restate the context it needs.
A session is refused instead while its earlier worker left a run, pause, action review, or outside operation unsettled; inspect its record and continue in a new session.

## Action Review Mode

`--review-mode`, `/permissions`, browser settings, and `PUT /settings/actions/review` refuse a mode for one of these reasons, shown with the mode:

| Cause | Resolution |
|---|---|
| The mode is outside the deployment's `allowed_action_review_modes` | Only the operator can change the ceiling; in a local launch the built-in policy lists only `review-all` and `review-changes`, so `review-outside-effects` is reported as not permitted by the deployment's policy |
| `review-outside-effects` without an admitted sandbox worker | Available only under a deployment manifest with a `[worker]` table; a local launch never runs one, so the mode is always unavailable there |
| Reduced review outside a sandbox worker with a credential-backed model route | See [`HW-CREDENTIAL-002`](#hw-credential-002-model-credential-isolation-is-unavailable) |
| A session is active and the selection asks less often than the current mode | Finish or resolve the active session work; a stricter mode is always accepted |

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
