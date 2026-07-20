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

Raw provider tokens are not written to `config.toml`, session events, logs, browser storage, or audit exports.
On supported workstations, Heartwood can store a token in the operating-system credential store after explicit confirmation; otherwise it keeps an entered token only for the running process or uses a deployment-provided secret binding.

## Persistence by Environment

| Environment | Durable Project Location |
|---|---|
| Workstation | The host folder mounted or used as the project |
| Container | The host folder mounted at `/workspace` |
| Terra | A dedicated directory below `/home/jupyter` on the persistent disk |
| Stanford Carina | A dedicated writable directory in approved project storage |

Job scratch, temporary container filesystems, and `/tmp` are not durable project locations.

## Back Up and Share Carefully

Back up research files according to institutional policy.
Treat `.heartwood/` as private operational state: it can contain prompts, paths, model metadata, tool summaries, and session history even though raw credentials are excluded.

Do not commit `.heartwood/` to source control or copy it into a public artifact.
Use an audit export when a reviewed, content-minimized record is needed.
