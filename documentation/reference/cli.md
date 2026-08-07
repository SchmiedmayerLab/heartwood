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
| `heartwood actions` | Show action-review modes |
| `heartwood actions set ask-every-time` | Require confirmation for every OpenHands action set |
| `heartwood actions set auto-approve-low-risk` | Auto-approve sets made entirely of low-risk actions when platform policy permits |

For unattended setup, use `heartwood setup --non-interactive --yes` with explicit model-source and model identifiers.
Use `--model-source heartwood` when Heartwood should download or import the model and supervise its runtime in the current environment.
Provider API keys are not accepted as setup command arguments.
Use `--model-source openai-subscription` for ChatGPT account access; the first setup remains interactive because OpenHands must display consent and complete account sign-in.

## Models

| Command | Purpose |
|---|---|
| `heartwood models list` | Show connections, credential status, profiles, and active model |
| `heartwood models refresh CONNECTION` | Refresh models exposed by a connection |
| `heartwood models connect CONNECTION MODEL` | Select a discovered model |
| `heartwood models validate [PROFILE]` | Evaluate credential and route policy for a profile |
| `heartwood models forget CONNECTION` | Remove a saved API key or OpenHands subscription credential |
| `heartwood models managed` | Show qualified recommendations, not-tested configurations, and user-selected models Heartwood can run |
| `heartwood models inspect OWNER/MODEL` | Inspect a public Hugging Face repository without downloading weights |
| `heartwood models download MODEL` | Download and select a recommendation or `OWNER/MODEL` repository |
| `heartwood models export PATH` | Verify and export the selected model as a portable bundle without replacing an existing file |
| `heartwood models inspect-bundle PATH` | Verify bundle structure and display model, source, license, runtime, size, and warnings without importing |
| `heartwood models import BUNDLE` | Review, verify, atomically import, and select a portable bundle |
| `heartwood models import PATH --source ...` | Copy and select a raw GGUF or vLLM snapshot with explicit provenance |

`models add`, `models select`, and `models remove` manage advanced non-secret LiteLLM-compatible profiles.
Use guided setup for normal provider and managed connections.

Bundle imports ask for explicit license approval.
Use `--approve-license` only in automation that already presented and approved the same bundle plan.
Export, inspection, import, progress, cancellation, and final selection use the same gateway contract in the terminal, browser, and notebook bridge.
See [Move a Model Into an Offline Environment](../models/offline.md) for the complete connected-to-offline workflow.

`heartwood models forget openai-subscription` signs out of ChatGPT for the current operating-system user.
The selected non-secret model profile remains in the project until another connection is selected.

## Skills

| Command | Purpose |
|---|---|
| `heartwood skills list` | List bundled, available, installed, revoked, and unsupported Skills |
| `heartwood skills refresh [--source ID]` | Refresh deployment-approved signed sources and apply revocations |
| `heartwood skills inspect NAME [--source ID]` | Review one current signed catalog entry without downloading it |
| `heartwood skills install NAME [--source ID]` | Show, confirm, reverify, and install one exact signed revision |
| `heartwood skills inspect-local PATH` | Validate an advanced local, unreviewed Agent Skill directory |
| `heartwood skills install-local PATH` | Show, confirm, and install the exact local directory as unreviewed content |
| `heartwood skills remove NAME` | Remove an installed extension |

The browser uses the same gateway operations and approval fields.
Source selection is required only when more than one signed source is configured.
For non-interactive use, add `--approve --expected-tree-sha256 sha256:DIGEST` with the complete digest returned by the matching inspect command.

## Research Specialists

| Command | Purpose |
|---|---|
| `heartwood specialists` | Show available advisory specialists and any disabled roles with their reason |

Inside an interactive terminal session, `/specialists` presents the same gateway-owned catalog.
Specialists are selected by the parent OpenHands agent during a task; this command inspects the available roles rather than launching a separate agent session.

## Session Automation

| Command | Purpose |
|---|---|
| `heartwood allow` | Allow the complete pending action set once |
| `heartwood reject` | Reject the complete pending action set |
| `heartwood pause` | Pause the selected session |
| `heartwood resume` | Resume the selected session |
| `heartwood replay` | Replay persisted events after audit verification |
| `heartwood audit export [--output PATH]` | Generate a scrubbed audit record and optionally copy it outside private project state |
| `heartwood audit verify` | Fully verify the paired session and audit history |

The aliases `approve` and `deny` remain command-line synonyms for automation.
Interactive users should use the visible controls or `/allow` and `/reject` without internal identifiers.

## Project Inspection

| Command | Purpose |
|---|---|
| `heartwood files list [DIRECTORY] [--depth N]` | List a bounded project tree |
| `heartwood files show FILE` | Print one bounded UTF-8 text file |
| `heartwood changes` | List Git changes or successful typed file actions for the selected non-Git session |
| `heartwood changes FILE` | Print one bounded read-only diff or non-Git current-file view |

These commands use the same gateway service as the full-screen terminal, browser, and notebook bridge.
They exclude private project state and return a nonzero status for unavailable, binary, or unsupported content.

## Operator Commands

| Command | Purpose |
|---|---|
| `heartwood runtime start` | Inspect and start the selected Heartwood-managed runtime, optionally requesting Slurm compute |
| `heartwood gateway serve` | Serve the gateway and packaged browser files without unified setup/runtime orchestration |
| `heartwood audit signer list` | List signer profiles approved by the deployment |
| `heartwood audit signer select PROFILE` | Select an approved signer profile for the current project |
| `heartwood audit signer default` | Return the project to the deployment default signer |
| `heartwood audit checkpoint ...` | Create a signed, canonical audit bundle outside the project |
| `heartwood audit verify-checkpoint BUNDLE [--public-key KEY]` | Verify a checkpoint against the active profile or an independently trusted public key |
| `heartwood signer init-local` | Initialize the explicit development and offline signer fallback outside the project |
| `heartwood signer serve-local` | Run the initialized signer as an authenticated loopback service |

These commands support deployment automation and diagnostics.
Researchers should normally use `heartwood` with `--interface` when needed.

Checkpoint creation requires an approved signer profile, output directory, deployment identifier, retention-policy identifier, and retention end date.
Private signing keys are never command arguments or project state.
It never replaces an existing output.
See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md) for the complete operator workflow and trust boundary.

`heartwood gateway serve` uses the detected platform's declared default ingress mode.
Workstations and Carina default to direct loopback and refuse a non-loopback bind.
A generic container can use a wildcard bind only when the operator explicitly declares loopback-only host publication.
Terra defaults to Jupyter proxy mode and requires its exact public origin and proxy base path.
Platform operators can select one explicit route:

| Option | Meaning |
|---|---|
| `--ingress-mode direct-loopback` | Accept direct requests through a loopback boundary; the default |
| `--ingress-mode jupyter-proxy` | Accept a loopback Jupyter proxy that strips one exact external prefix |
| `--ingress-mode trusted-proxy` | Accept only configured proxy source ranges and one complete forwarding set |
| `--public-origin ORIGIN` | Set the exact browser-visible `http` or `https` origin |
| `--base-path PATH` | Set the exact browser-visible gateway prefix |
| `--trusted-proxy-source IP_OR_CIDR` | Trust one proxy source; repeat the option for multiple exact ranges |
| `--trusted-identity-header NAME` | Require an additional non-secret proxy identity assertion header |
| `--trusted-identity VALUE` | Set the exact non-secret assertion value; configure it with the header |
| `--proxy-strips-prefix` | Declare that a trusted proxy removes the external prefix before forwarding |
| `--host-loopback-publication` | Assert that a wildcard container bind is published only on the host loopback interface |

The trusted identity assertion is a route marker, not a credential or substitute for proxy authentication.
The host-loopback assertion is valid only with a host mapping such as `-p 127.0.0.1:8767:8767`; it does not inspect or authenticate the container image.
The proxy must remove client-supplied forwarding and identity headers before setting its own values.
See [Add a Platform](../operate/platform-integration.md#gateway-ingress).

`heartwood runtime start --dry-run` prints the model, resource, and scheduler plan without downloading, starting inference, or requesting compute.
On a scheduler-managed GPU platform, `--task-profile standard|powerful|maximum` constrains automatic recommendation to the requested capability tier.
`--gpus` is an advanced constraint and must match a qualified catalog tensor-parallel configuration.
Unattended operation requires separate `--yes-download` and `--yes-request-allocation` approvals; neither is implied by `--non-interactive` or another confirmation flag.

## Exit Status

`0` indicates that the requested command completed successfully or an interactive cancellation changed no files.
Configuration, readiness, model, policy, or runtime failures return a nonzero status and print a recovery message.
Argument errors use standard command-line usage output.
