<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Configuration and State Reference

Heartwood derives the project from the process current directory.
Normal users do not set a Heartwood home, workspace, state root, model root, or session directory through environment variables.

## Project Layout

| Path | Contents |
|---|---|
| `.heartwood/config.toml` | Non-secret platform, model, action, policy, and Heartwood-managed model selection |
| `.heartwood/state.json` | State-schema marker |
| `.heartwood/sessions/` | Session metadata, events, audit chains, exports, and OpenHands persistence |
| `.heartwood/models/` | Downloaded or imported model artifacts and provenance |
| `.heartwood/skills/` | Explicitly installed project Skills |
| `.heartwood/audit/` | Project-level audit artifacts |
| `.heartwood/runtime/` | Runtime process and readiness state |
| `.heartwood/logs/` | Heartwood-managed inference and gateway diagnostics |
| `.heartwood/cache/` | Project-scoped model and runtime caches |

Heartwood creates the state root and children with private filesystem permissions and rejects symbolic-link substitutions.
The internal `.gitignore` excludes every state file from the surrounding Git repository.

## Configuration Ownership

Use the CLI or browser settings rather than editing `config.toml` manually.
Writes are validated, atomic, and protected by a project-scoped configuration lock.

The file may contain endpoint URLs, model identifiers, credential binding names, policy settings, and artifact provenance.
It must never contain raw credential values.

## Credential Binding Names

Built-in provider profiles use environment-style names such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and a platform-defined Stanford gateway binding.
The name identifies a secret source; it is not the secret.

On a supported workstation, an explicitly remembered value is stored by the operating-system keyring under a project-scoped account.
Containers and managed platforms normally use process entry, a mounted secret file, or managed identity.

## Deployment Environment

Environment variables remain valid at platform boundaries for detection, provider-secret injection, Jupyter routing, scheduler identity, GPU visibility, and packaged runtime wiring.
They are operator inputs rather than the normal researcher project-selection mechanism.

Common examples include platform markers, `GOOGLE_PROJECT`, `CLUSTER_NAME`, Slurm variables, CUDA visibility, and provider credential bindings.
Do not add these to shell history or documentation with real secret values.

## Concurrency

Configuration updates are serialized across concurrent Heartwood processes.
Session event files assume one writer process per session; use separate session identifiers or one owning gateway when multiple interfaces are needed.
