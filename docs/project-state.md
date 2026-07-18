<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Projects and Persistent State

Heartwood works on one project at a time. The project is exactly the directory where the Heartwood command, browser server, or notebook process starts.

## Set the Project Boundary

Enter the directory the agent may inspect and modify:

```bash
cd /path/to/analysis-project
heartwood
```

Heartwood does not search for a parent Git repository or require a workspace argument. Starting from a nested directory creates a separate project at that directory.

Heartwood file operations are confined to the project and exclude its private control directory. Terminal commands still run with the operating-system permissions of the Heartwood process. Use a platform sandbox when those broader process permissions are unacceptable.

## Understand the Private Directory

Heartwood creates `.heartwood/` inside the project:

```text
.heartwood/
├── config.toml
├── state.json
├── sessions/
├── models/
├── skills/
├── audit/
├── runtime/
├── logs/
└── cache/
```

| Path | Purpose |
|---|---|
| `config.toml` | Non-secret model, runtime, and action settings |
| `state.json` | Project-state format marker |
| `sessions/` | Conversations and agent execution state |
| `models/` | Downloaded model artifacts and provenance |
| `skills/` | Installed project extensions |
| `audit/` | Content-minimized activity records |
| `runtime/` | Local-runtime readiness information |
| `logs/` | Local-runtime diagnostics |
| `cache/` | Project-local transfer and runtime caches |

Do not commit, edit, or ask the agent to inspect `.heartwood/`. Provider token values are not stored there, but conversations and operational metadata may still be sensitive.

## Share State Across Interfaces

The terminal, browser, and notebook read the same project configuration and sessions. A model selected in the browser is visible to the next terminal command, and a terminal session can be replayed from a notebook.

Use one active writer for each session. Wait for a turn to finish before continuing that session from another process.

## Back Up or Move a Project

Stop Heartwood, then move or back up the project files and `.heartwood/` together. In a container or managed platform, keep the complete project on the persistent mount.

To start cleanly, create a new empty directory. Heartwood rejects unknown state layouts rather than guessing how to reinterpret them.

Downloaded models are project-local by default. Operators may mount dedicated persistent storage at `.heartwood/models/` while preserving the same user-visible layout.
