<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Command Reference

Run every command from the directory that should be the Heartwood project.
Use `heartwood COMMAND --help` for generated argument details in the installed release.

## Start Heartwood

```text
heartwood [--session-id ID] [--interface terminal|web] [--plain] [--prompt TEXT] [--port PORT]
```

| Option | Meaning |
|---|---|
| `--session-id ID` | Select a persistent conversation; default `session-main` |
| `--interface terminal` | Open the normal full-screen or fallback plain terminal |
| `--interface web` | Start the browser gateway and print the valid access route |
| `--plain` | Force the line-oriented terminal interface |
| `--prompt TEXT` | Submit one terminal task and exit |
| `--port PORT` | Set the browser gateway port; default `8767` |

`heartwood` performs project review, guided setup, Heartwood-managed runtime orchestration when required, and interface startup.

## Inspect and Configure

| Command | Purpose |
|---|---|
| `heartwood doctor [--json]` | Inspect content-safe project, model, credential, policy, and compute readiness without changing state |
| `heartwood setup` | Run model and action-policy setup without opening a conversation |
| `heartwood actions` | Show action-confirmation modes |
| `heartwood actions set ask-every-time` | Require confirmation for every OpenHands action set |
| `heartwood actions set auto-approve-low-risk` | Auto-approve low-risk sets when platform policy permits |

For unattended setup, use `heartwood setup --non-interactive --yes` with explicit model-source and model identifiers.
Use `--model-source heartwood` when Heartwood should download or import the model and supervise its runtime in the current environment.
Provider tokens are not accepted as setup command arguments.

## Models

| Command | Purpose |
|---|---|
| `heartwood models list` | Show connections, credential status, profiles, and active model |
| `heartwood models refresh CONNECTION` | Refresh models exposed by a connection |
| `heartwood models connect CONNECTION MODEL` | Select a discovered model |
| `heartwood models validate [PROFILE]` | Evaluate credential and route policy for a profile |
| `heartwood models forget CONNECTION` | Remove a saved provider credential from the system credential store |
| `heartwood models managed` | Show recommended and user-selected models Heartwood can run |
| `heartwood models inspect OWNER/MODEL` | Inspect a public Hugging Face repository without downloading weights |
| `heartwood models download MODEL` | Download and select a recommendation or `OWNER/MODEL` repository |
| `heartwood models import PATH ...` | Copy and select an existing GGUF or vLLM snapshot with provenance |

`models add`, `models select`, and `models remove` manage advanced non-secret LiteLLM-compatible profiles.
Use guided setup for normal provider and managed connections.

## Skills

| Command | Purpose |
|---|---|
| `heartwood skills list` | List bundled and installed Skills |
| `heartwood skills inspect PATH` | Validate and summarize a mounted Skill source |
| `heartwood skills install PATH --approve` | Install a reviewed extension into project state |
| `heartwood skills remove NAME` | Remove an installed extension |

## Session Automation

| Command | Purpose |
|---|---|
| `heartwood allow` | Allow the complete pending action set once |
| `heartwood reject` | Reject the complete pending action set |
| `heartwood pause` | Pause the selected session |
| `heartwood resume` | Resume the selected session |
| `heartwood replay` | Replay persisted events after audit verification |
| `heartwood audit export` | Export a scrubbed audit record |

The aliases `approve` and `deny` remain command-line synonyms for automation.
Interactive users should use the visible controls or `/allow` and `/reject` without internal identifiers.

## Operator Commands

| Command | Purpose |
|---|---|
| `heartwood runtime start` | Inspect and start the selected Heartwood-managed runtime, optionally requesting Slurm compute |
| `heartwood gateway serve` | Serve the gateway and packaged browser files without unified setup/runtime orchestration |

These commands support deployment automation and diagnostics.
Researchers should normally use `heartwood` with `--interface` when needed.

## Exit Status

`0` indicates that the requested command completed successfully or an interactive cancellation changed no files.
Configuration, readiness, model, policy, or runtime failures return a nonzero status and print a recovery message.
Argument errors use standard command-line usage output.
