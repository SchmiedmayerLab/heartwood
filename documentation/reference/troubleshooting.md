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
| `CREDENTIAL` | Provider token or managed-identity availability |
| `AGENT` | OpenHands agent runtime availability |
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

Reopen setup and enter the provider token, or ask the platform operator how its secret binding is supplied.
An operating-system keyring entry is project-scoped, so another project may correctly ask again.

### `HW-MODEL-002` — Heartwood-Managed Model Files Are Unavailable

Run:

```bash
heartwood models managed
heartwood models inspect OWNER/MODEL
```

Choose a supported candidate, confirm enough free disk, and download again.
For an offline transfer, use `heartwood models import` with an immutable revision and license record.

## Agent Runtime

### `HW-AGENT-001` — Agent Runtime Is Unavailable

The installed OpenHands dependency set cannot be loaded.
Run `heartwood --version`, reinstall the same Heartwood release through its documented installation route, and rerun `heartwood doctor`.
Model download, import, and inspection commands remain available so a broken agent runtime does not block project recovery.

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

## Collect Safe Diagnostics

Share the Heartwood version, `heartwood doctor --json`, platform, image tag or digest, failing `HW-*` code, and a minimal synthetic reproduction.
Remove project content, credentials, protected data, signed URLs, user identifiers, and unrestricted logs before attaching anything to a public issue.
