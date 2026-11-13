<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Projects and Private State

A Heartwood project is the directory from which you run `heartwood`.
The agent may work with files in that directory and its descendants; it does not search for a Git root or use a separately configured workspace path.

## Choose the Project Boundary

Create a dedicated folder containing only the code, allowed inputs, and outputs needed for the task.
Starting Heartwood from a nested analysis folder limits the project to that exact folder, even when a parent directory contains a Git repository.

Heartwood refuses obviously broad roots such as a user home directory or the Terra persistent-storage root.
Operating-system permissions and the surrounding platform remain the authoritative filesystem boundary.

## Heartwood Project State

After you confirm the project, Heartwood creates `.heartwood/`:

```text
project/
├── analysis files and folders
└── .heartwood/
    ├── config.toml
    ├── sessions/
    ├── models/
    ├── skills/
    ├── audit/
    ├── runtime/
    ├── logs/
    └── cache/
```

The folder has restrictive filesystem permissions and contains an internal `.gitignore` that ignores everything below it.
It stores non-secret configuration, sessions, downloaded or imported model files, installed Skills, runtime state, logs, caches, and audit artifacts.

Raw provider API keys and subscription tokens are not written to `config.toml`, session events, logs, browser storage, or audit exports.
On supported workstations, Heartwood can store an API key in the operating-system credential store after explicit confirmation; otherwise it keeps an entered key only for the running process or uses a deployment-provided secret binding.
ChatGPT account credentials remain in OpenHands' private user-level credential store rather than project state.

### Operator-Bound External State

An operator can launch the gateway with an external project deployment manifest using `heartwood gateway serve --deployment /absolute/path/project.toml`.
This mode keeps configuration, canonical sessions, audit records, installed Skills, models, runtime records, logs, and caches in the manifest's external state directory.
It does not create `.heartwood/` in the research directory or read project-local identity hints as authority.
A copied research directory needs its own authorized deployment binding.

The manifest must bind the exact canonical launch directory and contain a deny-by-default model-policy ceiling.
Its project identifier is a canonical UUID, and its external state directory must have that identifier as its final path component.
Both the manifest and the state directory must remain outside the research tree, with operator-owned parent directories that other users cannot replace.
The state parent must already exist.
Missing, malformed, mismatched, writable, or symlinked deployment configuration stops launch instead of falling back to local state.

Existing `.heartwood/state.json` prevents this mode from starting until an explicit state migration has preserved and verified the old records.
Do not remove that marker to bypass the check or point a new binding at an unverified copy.
External placement alone does not restrict same-identity terminal access; the execution environment still needs an independently enforced filesystem and process boundary.

A qualified deployment can add an explicit native worker selection to the same operator manifest.
The worker receives selected input snapshots and verified Skills through read-only volumes and keeps its writable work inside the sandbox.
It receives no research-directory bind mount, protected state directory, signing key or native control-plane credential.
The native execution path requires compatible admission APIs and a separately qualified provider route; it does not establish sensitive-data readiness merely by starting successfully.
See the [gateway reference](../reference/cli.md) for the manifest format.

## Persistence by Environment

| Environment | Durable Project Location |
|---|---|
| Workstation | The host folder mounted or used as the project |
| Container | The host folder mounted at `/workspace` |
| Terra | A dedicated directory below `/home/jupyter` on the persistent disk |
| Stanford Carina | A dedicated writable directory in approved project storage |

Job scratch, temporary container filesystems, and `/tmp` are not durable project locations.
The project location must also support native process-shared advisory file locks so an interrupted process releases session ownership automatically.
Use the attached Terra persistent disk or approved Carina project storage rather than an object-store mount for `.heartwood/`.

## Back Up and Share Carefully

Back up research files according to institutional policy.
Treat `.heartwood/` as private operational state: it can contain prompts, paths, model metadata, tool summaries, and session history even though raw credentials are excluded.

Do not commit `.heartwood/` to source control or copy it into a public artifact.
Use an audit export when a reviewed, content-minimized record is needed.
