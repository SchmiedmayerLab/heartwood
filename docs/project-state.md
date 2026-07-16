<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Project Files and State

Heartwood works on one project at a time. The project is exactly the directory where the Heartwood command, web server, or notebook process starts.

Most users only need to remember two rules: start Heartwood inside the directory it may modify, and preserve that complete directory when moving or backing up the project. The remaining sections document the storage contract for users and operators who need more detail.

## Choose the Project Boundary

Enter the directory the agent may inspect and modify before starting Heartwood:

```bash
cd /path/to/analysis-project
heartwood
```

Heartwood does not search parent directories, infer a Git repository root, or use a workspace argument. Starting from a nested directory creates a separate project at that directory. Run `pwd` first when the boundary matters.

Approved Heartwood file operations may address the project directory and its descendants. They may not address the reserved `.heartwood/` directory. OpenHands terminal commands still run with the operating-system permissions of the Heartwood process, so a platform sandbox is required when that broader process access is unacceptable.

## Understand `.heartwood/`

Heartwood creates one private control directory inside the project:

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
| `config.toml` | Non-secret model selection, connection bindings, action settings, and local runtime metadata |
| `state.json` | Version marker for the project-state layout |
| `sessions/` | Persisted conversations and OpenHands session state |
| `models/` | Downloaded model artifacts and provenance |
| `skills/` | Explicitly installed project extensions |
| `audit/` | Content-minimized, hash-chained audit records |
| `runtime/` | Runtime readiness and process metadata |
| `logs/` | Local runtime diagnostic logs |
| `cache/` | Project-local download and runtime caches |

Heartwood creates the directory with restrictive permissions and places a Git ignore rule inside it. Do not commit it, edit it manually, or ask the agent to inspect it. Although provider token values are not stored there, conversations and operational metadata may still be sensitive and must remain on storage appropriate for the project.

## Share State Across Interfaces

The terminal, browser, and notebook bridge resolve the same project and read the same `.heartwood/` state. A model selected in the browser is visible to the next terminal command; a terminal conversation can be replayed from a notebook; action settings apply to every interface.

Use one active writer for a session. Wait for an agent turn to finish before continuing that session from another process.

## Preserve or Reset a Project

To move a project, stop Heartwood and move the project files and `.heartwood/` together. On Terra or in a container, ensure the complete project directory is on the persistent disk or mount.

To start cleanly, create a new empty directory and start Heartwood there. Heartwood rejects an unknown or obsolete `.heartwood/` layout instead of guessing how to reinterpret it.

Downloaded model artifacts belong to the project by default. A deployment may mount dedicated persistent storage at `.heartwood/models/`, but users still interact with the same project layout and commands.

## Continue from Here

- Return to [Get Started](getting-started.md#step-2-create-or-open-a-project) to configure and use the project.
- Use [Browser and Notebooks](web-interface.md#continue-a-shared-session) when continuing the same session through another interface.
- Use [Deploy Heartwood](deployment.md#provide-durable-project-storage) when mapping the project to container, cloud, or scheduler-managed storage.
- Use [Troubleshooting](troubleshooting.md#resolve-project-and-persistence-problems) when interfaces resolve different projects or state does not survive a restart.
