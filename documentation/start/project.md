<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Projects and Private State

A Heartwood project is the directory from which you run `heartwood`.
The agent works with files in that directory and its descendants; Heartwood does not search for a Git root or read a separately configured workspace path.

## Choose the Project Boundary

Create a dedicated folder that contains only the code, permitted inputs and outputs the task needs.
Starting Heartwood from a nested analysis folder limits the project to that folder, even when a parent directory is a Git repository.

Heartwood refuses obviously broad roots such as a home directory or the Terra persistent-storage root.
Operating-system permissions and the surrounding platform remain the authoritative filesystem boundary.

## Heartwood Project State

In a local launch, confirming the project creates `.heartwood/`:

```text
project/
├── analysis files and folders
└── .heartwood/
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

The directory and its contents are owner-only, and an internal `.gitignore` ignores everything below it.
It holds non-secret configuration, sessions, downloaded or imported model files, installed Skills, runtime records, logs, caches and audit records.

Provider API keys and subscription tokens are not written to `config.toml`, session events, logs, browser storage or audit exports.
On workstations with a system credential store, Heartwood can save an API key there after explicit confirmation; otherwise an entered key lasts only for the running process, or the deployment supplies a secret binding.
ChatGPT sign-in credentials stay in OpenHands' user-level credential store, outside the project.

!!! warning "Local state is not protected from the agent"
    In a local launch, agent tools run with your permissions.
    An allowed terminal command can read and change `.heartwood/`, including settings, installed Skills, and session and audit files.
    Review any command that touches `.heartwood/` as a change to Heartwood's own controls.

### Operator-Bound External State

An operator can bind a project to protected state outside the research directory:

```bash
heartwood gateway serve --deployment /etc/heartwood/projects/research.toml
```

The deployment manifest binds the exact canonical research directory, a project identifier, an external state directory, a deny-by-default model policy and the review-mode ceiling.
Configuration, sessions, audit records, Skills, models, runtime records, logs and caches then live in the external state directory, and no `.heartwood/` is created in the research directory.
Project content is never read as deployment authority, and a copied research directory needs its own binding.

The launch stops instead of falling back to local state when the manifest or state directory is missing, malformed, mismatched, writable by others or a symbolic link.
The project identifier is a canonical UUID and is the final path component of the state directory.
Manifest and state directory both stay outside the research tree, under operator-owned parents that other users cannot replace; the state parent must already exist.
An existing `.heartwood/state.json` stops the launch until its records are migrated as described below.

Only `heartwood gateway serve` and the `heartwood project` migration commands accept `--deployment`.
Standalone terminal, notebook and maintenance commands do not discover the binding, so researchers use the bound gateway or the operator's integration.
External placement alone does not stop a process with the same operating-system identity from reaching the state; the [isolated worker](../operate/security.md#isolated-worker) is the deployment that keeps agent code away from it.
[Configuration and State](../reference/configuration.md#deployment-manifest) lists the manifest keys, including the `[worker]` table.

#### Moving Existing Local State

Stop the project's gateways, model runtimes and recorded experiments, then run the migration from the research directory with the operator manifest and a new backup path:

```bash
heartwood project migrate --deployment /etc/heartwood/project.toml --backup /srv/heartwood/backups/project-original
```

The backup must be on the research directory's filesystem so that the original state can be retired atomically; the destination can be on another filesystem.
Allocate space for a complete copy of the state, including model files.
Migration verifies the session and audit chains, command receipts, preferences, experiment journal and installed Skill content before it activates the protected state, and stops on symbolic links, multiply linked files and special files.
The complete original directory is retained as the backup, and relocation does not change the assurance of historical records.

After retirement, a `.heartwood-migration.json` file in the research directory prevents local startup; do not delete it to reinitialize the project.
If retirement or activation is interrupted, both startup paths stay closed until an operator inspects the retained evidence and resumes:

```bash
heartwood project recover-migration --deployment /etc/heartwood/project.toml
```

Add `--rollback` to restore the original local state, which is refused once the protected project has changed or when local state already exists.
Backups and failed staging directories are removed only by explicit operator cleanup.

## Persistence by Environment

| Environment | Durable Project Location |
|---|---|
| Workstation | The folder used as the project |
| Container | The host folder mounted at `/workspace` |
| Terra | A dedicated directory below `/home/jupyter` on the persistent disk |
| Stanford Carina | A dedicated writable directory in approved project storage |
| Operator-bound deployment | The research directory, plus the manifest's external state directory |

Job scratch space, temporary container filesystems and `/tmp` are not durable project locations.
Project state requires native process-shared advisory file locks, so use a persistent disk or native project filesystem rather than an object-store mount.

## Back Up and Share Carefully

Back up research files according to institutional policy, together with the matching state: `.heartwood/` or the operator-bound state directory.
That state can contain prompts, paths, model metadata, tool summaries and session history even though credentials are excluded.
Do not commit it to source control or copy it into a public artifact; use an [audit export](../use/actions-audit.md#audit-export) when a reviewed, content-minimized record is needed.
