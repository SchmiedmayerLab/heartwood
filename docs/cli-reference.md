<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Command Reference

Run `heartwood --help` or `heartwood <command> --help` for the authoritative options in the installed release.

## Everyday Commands

| Command | Purpose |
|---|---|
| `heartwood` | Configure the project when needed, then open the interactive terminal |
| `heartwood doctor` | Inspect project, model, policy, and compute readiness |
% TODO: I why is this only for local models? Coundn't this just be used to start heartwood? What's different to the main command and chat? I don't fully get this? 
| `heartwood launch` | Start a downloaded model and open the terminal; request Carina compute when needed |
| `heartwood serve` | Serve the browser for a hosted or already-running model |
% TODO: So this is lauch and serve at the same; why do these three commands exist?
| `heartwood launch --web` | Start a downloaded model and serve the browser |
% TODO: Not fully get this, what does replay does?
| `heartwood replay` | Replay the default session |
| `heartwood audit export` | Export the scrubbed audit record |

Use `--session-id <name>` before a command to select another session.

## Session Commands

| Command | Purpose |
|---|---|
| `heartwood chat` | Open the conversation |
| `heartwood chat --plain` | Use the line-oriented conversation |
| `heartwood chat --prompt "<task>"` | Submit one task without opening the prompt loop |
| `heartwood allow` | Allow the complete pending action group once |
| `heartwood reject` | Reject the complete pending action group |
| `heartwood pause` | Pause the session |
| `heartwood resume` | Resume the session |

## Model Commands

| Command | Purpose |
|---|---|
| `heartwood models list` | Show available connections and the active profile |
| `heartwood models refresh <connection>` | Request the connection's current model list |
| `heartwood models connect <connection> <model>` | Select and activate a discovered model |
| `heartwood models local` | List local models compatible with the packaged runtime |
| `heartwood models inspect <owner/model>` | Build a plan for another Hugging Face repository |
| `heartwood models download <model>` | Download and verify a listed or inspected model |
| `heartwood models validate` | Check credentials and route authorization |

`models add`, `models select`, and `models remove` are advanced profile operations.

## Skills and Actions

| Command | Purpose |
|---|---|
| `heartwood skills list` | List bundled and installed Skills |
| `heartwood skills inspect <path>` | Validate a mounted Skill source |
| `heartwood skills install <path> --approve` | Install a reviewed project extension |
| `heartwood skills remove <name>` | Remove an installed extension |
| `heartwood actions set always-confirm` | Ask before every action group |
| `heartwood actions set confirm-risky` | Auto-approve low-risk groups when platform policy permits |

## Diagnostics and Reviewer Tools

`heartwood detect` reports platform and current synthetic detector evidence without running code. It does not identify a real biomedical dataset.

`heartwood doctor --json` prints machine-readable readiness for automation and support tooling.
