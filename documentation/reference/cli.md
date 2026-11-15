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

`/workflow` opens research workflow setup or stage review in the full-screen terminal.
In plain mode, `/workflows` lists definitions and `/workflow` shows the current stage and available commands.
Use `/experiments` to inspect recorded stage identities, output fingerprints, and links to recorded actions in the current session.
See [Experiment Records](../architecture/experiments.md) for project-wide queries and export.
See [Research Workflows](../use/research-workflows.md) for input requirements, stage controls, and completion checks.
Workflows with unimplemented checks are marked unavailable.
Specialists are selected by the parent OpenHands agent during a task; this command inspects the available roles rather than launching a separate agent session.

## Experiment Records

| Command | Purpose |
|---|---|
| `heartwood experiments record --input FILE --output NEW_FILE SCRIPT [ARGS...]` | Record an explicit user-run analysis using Heartwood's Python |
| `heartwood experiments list [--json]` | Inspect project-wide workflow and script records without starting a model |
| `heartwood experiments export` | Write verified scientific record JSONL to standard output |
| `heartwood experiments environment [--python EXECUTABLE]` | Export Python version metadata; defaults to Heartwood's interpreter, not an unrelated analysis environment |
| `heartwood experiments recover RUN_ID` | Mark an abandoned script attempt interrupted without executing or stopping it |
| `heartwood experiments cancel RUN_ID` | Close an abandoned script record without deleting outputs or stopping processes |

Put recorder options before `SCRIPT`; following arguments belong to the script.
Repeat `--input`, `--output`, and `--code` for dependencies and outputs.
An alternative `--runner` requires a declared `--environment-sha256`.
Script recording is not agent execution or a sandbox, and scientific records are not signed audit exports.
See [Experiment Records](../architecture/experiments.md) for examples, evidence boundaries, and safe recovery.

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
| `heartwood audit checkpoint ... [--include-experiments]` | Create a signed audit bundle outside the project, optionally retaining the exact project experiment export |
| `heartwood audit verify-checkpoint BUNDLE [--public-key KEY]` | Verify a checkpoint against the active profile or an independently trusted public key |
| `heartwood signer init-local` | Initialize the explicit development and offline signer fallback outside the project |
| `heartwood signer serve-local` | Run the initialized signer on its owner-only Unix socket |

These commands support deployment automation and diagnostics.
Researchers should normally use `heartwood` with `--interface` when needed.

Checkpoint creation requires an approved signer profile, output directory, deployment identifier, retention-policy identifier, and retention end date.
Private signing keys are never command arguments or project state.
It never replaces an existing output.
See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md) for the complete operator workflow and trust boundary.

`heartwood gateway serve` prints one launch link after binding and listening on its configured port.
Open that link once; the page exchanges its fragment token for a tab-scoped gateway capability used by API requests and event streams.
The link is invalid afterwards, but the same tab retains access across reloads while the gateway runs.
Automation can supply a random token of at least 32 printable ASCII characters through `--capability-fd N`, where `N` is an inherited file or pipe descriptor numbered at least 3.
For example, `heartwood gateway serve --capability-fd 3 3</run/secrets/heartwood-capability` reads an operator-protected secret file without putting its contents in arguments or the environment.
The gateway consumes at most 4096 bytes, accepts one optional trailing newline, requires pipe EOF within five seconds, closes the descriptor, and prints no launch link.
The proxy or automation client sends the same secret in the `X-Heartwood-Capability` header.
The source must remain outside worker access; environment capability delivery is rejected.
Restart the gateway to obtain a new link.

An operator can bind canonical state and model policy outside the research directory:

```bash
heartwood gateway serve --deployment /etc/heartwood/projects/research.toml
```

The manifest uses this format, with deployment-specific canonical paths and approved endpoints:

```toml
schema_version = "heartwood.project-deployment.v1"
project_id = "93d29ccd-6177-4817-9806-44ea579818c9"
workspace = "/research/project"
state_root = "/var/lib/heartwood/projects/93d29ccd-6177-4817-9806-44ea579818c9"

[policy]
policy_id = "deployment-models"
platform_id = "generic"
deny_egress_by_default = true
allowed_model_endpoints = ["https://models.example.invalid/v1/chat/completions"]
allowed_model_catalog_endpoints = ["https://models.example.invalid/v1/models"]
allowed_capability_tiers = ["supervised"]
allowed_action_confirmation_modes = ["always-confirm"]
credential_allowlist = ["HEARTWOOD_CUSTOM_MODEL_API_KEY"]
aggregate_count_floor = 20
```

Credential names are references; actual credentials never belong in this manifest.
Launch from the manifest's exact research directory and use the detected platform identifier.
Keep the manifest operator-owned and not writable by group or other users, and exclude it and the external state volume from worker mounts.
Only a trusted launcher selects `--deployment`; no browser request or project-local hint can replace its policy or paths.
See [Projects and Private State](../start/project.md#operator-bound-external-state) for path, ownership, and legacy-state requirements.

A qualified operator deployment can add the following worker selection.
Replace the example identities and hashes with independently qualified values; the effective policy hash must not be learned from the same worker whose admission is being decided.
The policy file is native sandbox-policy JSON, while `native_config_home` contains the native CLI's registered gateway metadata and mutual TLS files.
All native tools, configuration and the local Docker socket must have trusted ownership and paths outside research files and project state.
If the socket grants group write access, the operator must explicitly set `docker_socket_group` to its trusted numeric group ID.
Membership in that group grants Docker control-plane authority; never add a worker identity to it.
A changed group or world-writable socket is rejected.

```toml
[worker]
driver = "openshell-docker"
gateway = "research-workers"
native_config_home = "/etc/heartwood/native"
openshell_cli = "/opt/heartwood/bin/openshell"
docker_cli = "/opt/heartwood/bin/docker"
docker_host = "unix:///run/docker.sock"
workspace = "research"
image_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
sandbox_policy_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
policy_path = "/etc/heartwood/native/worker-policy.json"
policy_file_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
profile_id = "approved-research"
model = "provider/approved-model"
model_endpoint = "https://models.example.invalid/v1/chat/completions"
provider = "approved-research"
placeholder_env = "HEARTWOOD_CUSTOM_MODEL_API_KEY"
cpu = "1"
memory = "2Gi"

[worker.inputs]
schema_version = "heartwood.worker-inputs.v1"
files = []
```

The selected application profile must match the configured profile, model and endpoint, and remain allowed by the deployment policy.
Each selected input entry declares a project-relative `path`, `sha256` and `size_bytes`; an empty selection transfers no research files.
The launcher projects verified Skills separately and binds the complete selection to the retained worker attempt.
Provider attachments supply placeholders, not real inference secrets, to the worker.
The qualified inference endpoint must independently enforce approved models and tenant scope; this manifest does not add a model router or override provider authorization.
Unavailable native dependencies, unsupported admission hooks, mismatched observed policy or failed outside revocation stop the operation without local fallback.
Stopping an uncertain worker requires outside reconciliation; do not delete its records or release its writer merely because a client disconnected.

The same manifest can select automatic checkpointing through an `evidence` table.
Evidence configuration requires complete worker isolation and explicitly binds the records service, credential source, retention and approved production signer profile.
The operator-owned records client is closed when gateway serving exits, including failed startup.
See [Configure Managed Evidence](../operate/audit-checkpoints.md#configure-managed-evidence) for the complete table and storage permissions.

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
