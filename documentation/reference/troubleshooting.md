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

Each warning or failure includes a stable `HW-*` code, plain-language title, next action, and documentation route.
Use `heartwood doctor --json` when an administrator needs the structured report; review paths before sharing it.

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
| `ENV` | Conditions that cannot yet be classified more precisely |

Numbers from `001` through `899` are assigned sequentially within that area and are never reused for a different condition.
Numbers from `900` through `999` are reserved for generic fallbacks when Heartwood cannot classify a condition more precisely.
A gap can therefore represent a retired code rather than an omitted priority level.

Warning and failure status is reported separately from the code, so a numeric suffix does not imply urgency.

## Project Storage

### `HW-PROJECT-001` — Project Storage Is Unavailable

Enter an existing writable directory dedicated to the analysis.
Check the path and permissions:

```bash
pwd
ls -ld .
```

On Terra, use a child directory below `/home/jupyter`.
On Carina, use an approved writable project directory below `/projects`.

### `HW-PROJECT-002` — Project Setup Needs Attention

Run `heartwood` from the intended project and confirm **Use this project**.
If `.heartwood/` already exists but has no valid state marker, move it aside only after confirming it does not contain needed state; do not let Heartwood overwrite an unknown directory.

### `HW-PROJECT-003` — Choose a Dedicated Project Directory

Heartwood refuses a filesystem root or home directory because that boundary is too broad.
Create and enter a child folder, then run `heartwood doctor` again.

## Configuration

### `HW-SETUP-001` — Project Configuration Needs Attention

Run `heartwood setup` or open browser settings and choose a model connection.
Do not repair `config.toml` by inserting a token or copying a profile from another platform.

### `HW-SETUP-002` — Model and Policy Settings Do Not Agree

Open setup and select the intended model connection again.
This regenerates the non-secret model profile and matching platform policy as one update.

## Models and Credentials

### `HW-MODEL-001` — No Model Is Selected

Run `heartwood` and choose a model returned by an available connection.
For Heartwood-managed inference, download or import the model before starting the agent.

### `HW-CREDENTIAL-001` — Model Credential Is Unavailable

Reopen setup and enter the provider API key, or ask the platform operator how its secret binding is supplied.
An operating-system keyring entry is project-scoped, so another project may correctly ask again.
For **Sign in with ChatGPT**, repeat the one-time-code flow.
If the account should no longer be used, run `heartwood models forget openai-subscription` before signing in with another account.

### `HW-CREDENTIAL-002` — Model Credential Isolation Is Unavailable

The selected model uses a credential, but the active platform provides application scrubbing rather than a separate model-only credential boundary.
Select **Review Every Action**, or choose a credential-free model route.
Low-Risk Automation becomes available for a secret-backed route only when the platform adapter reports live-qualified isolation.

Application scrubbing still keeps the credential out of supported tool inputs, project state, browser and notebook output, session events, logs, and audit exports.
It does not isolate same-identity process memory or every operating-system resource.
See [Security and Controlled Data](../operate/security.md#model-credential-isolation).

### `HW-MODEL-002` — Heartwood-Managed Model Files Are Unavailable

Run:

```bash
heartwood models managed
heartwood models inspect OWNER/MODEL
```

Choose a Qualified configuration, confirm enough free disk, and download again.
For an offline transfer, use `heartwood models import` with an immutable revision and license record.

## Agent Runtime

### `HW-AGENT-001` — Agent Runtime Is Unavailable

The installed OpenHands dependency set cannot be loaded.
Run `heartwood --version`, reinstall the same Heartwood release through its documented installation route, and rerun `heartwood doctor`.
Model download, import, and inspection commands remain available so a broken agent runtime does not block project recovery.

### `HW-AGENT-002` — An Agent Action Failed

Heartwood received a failed action from the OpenHands runtime.
Review the proposed action set and model connection in Activity & audit, then try the task again.
Heartwood deliberately omits provider and project content from this diagnostic.

### `HW-AGENT-003` — The Agent Conversation Stopped

OpenHands stopped the conversation before the task completed.
Review Activity & audit, verify the model connection, and start the task again.

### `HW-AGENT-004` — The Agent Worker Stopped

The background OpenHands worker stopped unexpectedly.
Run `heartwood doctor`, review Activity & audit, and try the task again.

### `HW-AGENT-005` — The Agent Cannot Perform That Operation

The requested operation does not match the conversation's current state.
Review the active task or pending action set before trying the operation again.

### `HW-AGENT-006` — An Approved Action Has an Unknown Outcome

Heartwood stopped after an action was approved but before OpenHands recorded whether it completed.
Do not approve or repeat the action blindly.
Inspect the project files and Activity & audit, then continue in a new session once you understand the project state.

### `HW-AGENT-007` — An Agent Turn Has an Unknown Outcome

Heartwood stopped during a model turn before OpenHands recorded a stable completion boundary.
Do not repeat the task blindly because the provider may already have processed the request.
Inspect the session replay and Activity & audit, then continue in a new session.

### `HW-AGENT-008` — Model Provider Authentication Failed

The model provider rejected the credential configured for the active connection.
Open model settings, update the credential through the supported secret mechanism, and validate the connection before retrying.

### `HW-AGENT-009` — Model Provider Quota Exhausted

The provider reported that the configured quota, credit, or budget is exhausted.
Review the approved provider account or select another approved model connection before continuing.

### `HW-AGENT-010` — Model Provider Rate Limited

The provider temporarily limited requests.
Wait briefly and retry the task without repeating any action whose outcome is unknown.

### `HW-AGENT-011` — Model Configuration Is Invalid

The selected model, endpoint, or provider configuration cannot serve the requested task.
Open model settings, correct the connection, and validate it before retrying.

### `HW-AGENT-012` — Model Provider Is Unavailable

The configured model service could not be reached or returned a temporary availability failure.
Check the connection and provider status, then retry the task.

### `HW-AGENT-999` — The Agent Runtime Reported an Error

An execution backend returned an error without a more specific stable code.
Review Activity & audit, run `heartwood doctor`, and try the task again.

## Project Inspection

### `HW-WORKSPACE-001` — Project Path Is Invalid

Use a normalized path relative to the current project.
Do not use an absolute path, `..`, backslashes, repeated separators, control characters, or a trailing separator.

### `HW-WORKSPACE-002` — Private Project State Is Not Available

The Files and Changes interfaces never expose `.heartwood/` or `.git/`, including nested directories with either name.
Use the documented audit export or Git commands from a trusted terminal when you are authorized to inspect that state.

### `HW-WORKSPACE-003` — Symbolic Link Inspection Is Not Available

Heartwood does not follow symbolic links through its read-only project API.
Inspect the intended regular file through its project-relative path.

### `HW-WORKSPACE-004` — Expected a Directory

Use `heartwood files show FILE` for a file or choose a directory for `heartwood files list`.

### `HW-WORKSPACE-005` — Project Entry Is Unavailable

The path does not exist or its metadata cannot be read.
Confirm the spelling and project permissions, then retry from the same project directory.

### `HW-WORKSPACE-006` — Requested Tree Depth Is Unsupported

Choose a positive depth no greater than the limit reported by the workspace response.
Omit `--depth` to use the default bounded depth.

## Managed Compute

### `HW-COMPUTE-001` — A Compute Allocation May Be Required

On Carina, start `heartwood`, inspect the full Slurm request, and approve it only when the resources and duration are appropriate.
Use `heartwood runtime start --dry-run` to inspect without allocating.

### `HW-COMPUTE-002` — Allocation Scratch Storage Is Unavailable

Request an allocation that provides writable job scratch or use the platform's supported runtime path.
Keep durable project and model state in approved project storage.

### `HW-COMPUTE-003` — A Compatible GPU Is Unavailable

Choose a hosted model, a GGUF CPU model, or GPU-enabled compute.
For containers, verify the NVIDIA Container Toolkit and `--gpus all`; for Terra, select the GPU image and attach an NVIDIA GPU.
The CUDA 12.9 runtime requires compute capability 7.5 or newer, so P4, P100, and V100 GPUs are rejected before model startup.
Compare the detected environment with [GPU Compatibility](gpu-compatibility.md).

## Terra

### `HW-TERRA-001` — Choose a Dedicated Terra Project Directory

```bash
mkdir -p /home/jupyter/heartwood-project
cd /home/jupyter/heartwood-project
heartwood doctor
```

Do not use `/home/jupyter` itself as the agent boundary.

### `HW-TERRA-002` — Terra GPU Support Is Unavailable

Use the `-terra-gpu-nvidia` image and attach supported GPU compute, or choose hosted inference.
Delete and recreate the Cloud Environment with a T4 while retaining the persistent disk; Terra does not apply a changed image or GPU selection to an existing environment.

## Environment Fallback

### `HW-ENV-999` — Environment Check Needs Attention

Heartwood encountered a readiness check that has no more specific public diagnostic.
Run `heartwood doctor`, inspect the failed check and its next action, and include the structured `heartwood doctor --json` output in a synthetic issue report if the condition persists.

## Gateway Ingress

### `HW-INGRESS-001` — Gateway Ingress Configuration Is Unsafe

The requested bind, origin, prefix, or proxy trust values do not form one safe route.
Use the default loopback gateway for direct access.
For a Jupyter or trusted platform proxy, configure the exact browser-visible origin and base path, the expected prefix behavior, and the trusted proxy source boundary.

Do not solve this diagnostic by widening the bind or using a wildcard origin.
See [Add a Platform](../operate/platform-integration.md#gateway-ingress).

### `HW-INGRESS-002` — Gateway Request Does Not Match the Configured Route

Use the exact URL supplied by the deployment.
If the route is operator-managed, verify the upstream source address, external host and protocol, stripped or preserved prefix, HTTP and WebSocket origins, and complete forwarded-header set.

The proxy must remove forwarding and identity headers supplied by the client before adding its own values.
Do not disable host, origin, or path validation to make the request pass.

## Browser Access

If the browser page does not open:

1. keep the launching terminal running;
2. confirm `heartwood --interface web` reported ready;
3. use the exact printed URL;
4. check whether port `8767` is already in use; and
5. run `heartwood doctor` from the same directory.

The browser interface is not supported on Terra or Stanford Carina.
Use the terminal or the Terra notebook interface instead of constructing a proxy path.

Do not work around a proxy failure by binding the gateway publicly without authentication.

## Session Ownership and Interrupted Commands

If Heartwood reports that a session is active in another process, stop the terminal, browser server, or notebook kernel that owns it and try again.
Do not remove `.writer.lock` or `.writer.json`; the operating-system lock determines ownership, and Heartwood reclaims stale metadata after the prior process has exited.
If Heartwood reports that the project storage does not support required process locks, move the complete project to a native local, attached persistent, or qualified project filesystem.
Do not move only `.heartwood/`, and do not use an object-store mount as session storage.

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

A checkpoint output and signing key must be outside the Heartwood project.
Use an owner-only Ed25519 private key, a new output directory, a valid retention date on or after checkpoint creation, and storage that supports native process-shared locks.
Verify a restored bundle with the independently retained public key before relying on it.

See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md) for the complete workflow and the limits of the signed retention declaration.

## Collect Safe Diagnostics

Share the Heartwood version, `heartwood doctor --json`, platform, image tag or digest, failing `HW-*` code, and a minimal synthetic reproduction.
Remove project content, credentials, protected data, signed URLs, user identifiers, and unrestricted logs before attaching anything to a public issue.
