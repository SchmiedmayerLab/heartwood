<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Command Reference

Run every command from the directory that should be the Heartwood project.
`heartwood COMMAND --help` prints the arguments of the installed release.

## Start Heartwood

```text
heartwood [--session-id ID] [--interface {terminal,web}] [--plain]
          [--review-mode {review-all,review-changes,review-outside-effects}]
          [--prompt PROMPT] [--port PORT] [--host HOST] [--host-loopback-publication]
```

| Option | Meaning |
|---|---|
| `--session-id ID` | Persistent conversation to open; default `session-main` |
| `--interface terminal` | Full-screen terminal, or the plain terminal when the full-screen interface is unavailable; the default |
| `--interface web` | Start the browser gateway and print its one-time launch link |
| `--plain` | Line-oriented terminal; only with `--interface terminal` |
| `--review-mode MODE` | Action-review mode for this launch; see [Review Mode Selection](#review-mode-selection) |
| `--prompt PROMPT`, `-p PROMPT` | Submit one task in the terminal and exit |
| `--port PORT` | Browser gateway port, 1 to 65535; default `8767` |
| `--host HOST` | Browser gateway bind host; default `127.0.0.1`; not shown by `--help`, and a wildcard such as `0.0.0.0` also needs `--host-loopback-publication` |
| `--host-loopback-publication` | Assert that a wildcard container bind is published only on the host loopback interface |
| `--version` | Print the installed version |

`heartwood` reviews the project, runs guided setup when a model or credential is missing, starts a Heartwood-managed runtime when one is required, and opens the selected interface.
Without a terminal on standard input and without `--prompt`, it prints usage and exits.

## Inspect and Configure

| Command | Purpose |
|---|---|
| `heartwood doctor [--json] [--deployment MANIFEST]` | Report project, model, credential, policy, and compute readiness without changing state; with a deployment manifest, report its effective policy and source, and check the selected model route and stored review mode against that policy |
| `heartwood setup [--deployment MANIFEST]` | Configure a model connection without opening a conversation; with a deployment manifest, configure the deployment's project and also list the model connections the manifest declares |
| `heartwood actions` | Show the review mode in use, the default, its scope, and why any mode is unavailable |
| `heartwood specialists` | List research specialists and any disabled role with its reason |

`heartwood setup` options:

| Option | Meaning |
|---|---|
| `--model-source SOURCE` | `heartwood`, `stanford-ai-api-gateway`, `openai-subscription`, `openai`, `anthropic`, `custom`, or with `--deployment` a connection identifier the manifest declares. Setup stops with [`HW-SETUP-004`](troubleshooting.md#hw-setup-004-capability-is-not-offered-on-this-platform) for a source the platform does not offer |
| `--deployment MANIFEST` | Absolute operator manifest whose project and model connections to configure |
| `--model-id ID` | Exact model identifier reported by the service |
| `--base-url URL` | Base URL of another compatible model service |
| `--remember-credential` | Store the provider API key in the system credential store when one is available |
| `--non-interactive` | Require explicit inputs and never prompt |
| `--yes` | Confirm the displayed configuration |
| `--yes-download` | Confirm the displayed model download |

Provider API keys are never command arguments.
`--model-source heartwood` downloads or imports a model and supervises its runtime in the current environment.
The first `openai-subscription` setup stays interactive because OpenHands displays consent and completes the account sign-in.

### Review Mode Selection

The three modes are `review-all`, `review-changes`, and `review-outside-effects`; see [Review Modes](../use/actions-audit.md#review-modes).
The gateway resolves a selection against three limits and stops with the reason when one excludes it:

- the deployment's `allowed_action_review_modes` ceiling;
- `review-outside-effects` requires sessions that run in an isolated worker;
- outside an isolated worker, any mode other than `review-all` requires a credential-free or platform-isolated model route.

A local launch never runs an isolated worker, so `review-outside-effects` is available only under `gateway serve --deployment` with a `[worker]` table.
Reducing review is refused while a session is active; a stricter mode can be selected at any time.

| Form | Scope |
|---|---|
| `heartwood --review-mode MODE` | This terminal or browser launch |
| `heartwood --review-mode MODE runtime start` | The interface started after the managed runtime |
| `heartwood gateway serve --review-mode MODE` | This gateway process; with `--deployment`, stored in protected project state |
| `/permissions MODE` in the terminal | This terminal launch |
| Browser settings | The serving gateway process; stored under `gateway serve --deployment` |

Without a deployment manifest, a selection lasts until the process exits and is never written to the research directory.
`--review-mode` is rejected on commands that do not start an interface.

## Models

| Command | Purpose |
|---|---|
| `heartwood models list` | Connections, credential status, profiles, and the active model |
| `heartwood models refresh [--base-url URL] CONNECTION` | List every model a connection currently exposes |
| `heartwood models connect [--base-url URL] [--manual] CONNECTION MODEL` | Select and activate one discovered model; `--manual` accepts a Custom API identifier the server cannot list |
| `heartwood models validate [PROFILE]` | Check credential availability and platform route authorization |
| `heartwood models forget CONNECTION` | Remove a saved API key or OpenHands subscription credential |
| `heartwood models managed` | Models Heartwood can run in this environment, with qualification status |
| `heartwood models inspect [--revision REV] OWNER/MODEL` | Inspect supported models in a Hugging Face repository without downloading weights |
| `heartwood models download [--revision REV] MODEL` | Download and select a recommendation or `OWNER/MODEL` repository |
| `heartwood models export PATH` | Export the selected model as a verified bundle to a new path |
| `heartwood models inspect-bundle PATH` | Verify a bundle and show its model, source, license, runtime, size, and warnings without importing |
| `heartwood models import [--approve-license] PATH` | Verify, import, and select a Heartwood bundle |
| `heartwood models import --source OWNER/MODEL --revision COMMIT --license TEXT [--context-window N] PATH` | Copy and select a raw GGUF or vLLM snapshot with explicit provenance |
| `heartwood models add PROFILE --model MODEL --policy-endpoint URL [...]` | Advanced: add or update a non-secret LiteLLM profile |
| `heartwood models select PROFILE` | Advanced: select a saved profile |
| `heartwood models remove PROFILE` | Remove a profile |

`models add` also accepts `--base-url`, `--credential-kind {environment,file,managed-identity,none}`, `--api-key-env`, `--api-key-file`, `--api-version`, `--aws-region-name`, `--aws-profile-name`, `--capability-tier {autonomous,supervised,experimental}`, `--description`, and `--select`.
Use `--approve-license` only in automation that already presented and approved the same bundle.
`heartwood models forget openai-subscription` signs the current operating-system user out of ChatGPT; the project keeps its non-secret profile until another connection is selected.
See [Move a Model Into an Offline Environment](../models/offline.md) for the bundle workflow.

## Research Services

| Command | Purpose |
|---|---|
| `heartwood services list` | Servers, token state, reviewed tools and the tools enabled for new sessions |
| `heartwood services add --label TEXT [--token-stdin] [--remember] ID URL` | Add a server at a deployment-approved origin; it stays disabled |
| `heartwood services test [--token-stdin] [--remember] ID` | Connect and list the server's tools; listing authorizes nothing |
| `heartwood services select ID [TOOL=SHA256 ...]` | Select tools with the definition digests printed by `services test`; no tool removes the selection |
| `heartwood services enable ID`, `disable ID` | Offer or stop offering the selected tools to new sessions |
| `heartwood services disconnect ID` | Remove the server and forget its token; completed remote work is not undone |

Pass `--deployment PATH` before the subcommand, as in `heartwood services --deployment PATH list`; its `mcp_destinations` list the allowed origins, and without it every server is refused.
`--token-stdin` reads the bearer token from the first line of standard input.
Without `--remember`, that token lasts only for the command; keep it with `--remember` where the deployment allows the system credential store, or have the operator set the variable named by `services list`.
Tools reach sessions started by `heartwood gateway serve --deployment` with an isolated worker; see [Research MCP Servers](../operate/platform-contract.md#research-mcp-servers).

## Skills

| Command | Purpose |
|---|---|
| `heartwood skills list` | Bundled, available, installed, revoked, and unsupported Skills |
| `heartwood skills refresh [--source ID]` | Refresh deployment-approved signed sources and apply revocations |
| `heartwood skills inspect [--source ID] NAME` | Review one signed catalog entry without installing it |
| `heartwood skills install [--source ID] [--approve --expected-tree-sha256 SHA256] NAME` | Install one exact signed revision after review |
| `heartwood skills inspect-local SOURCE` | Inspect a local, unreviewed Agent Skill directory |
| `heartwood skills install-local [--approve --expected-tree-sha256 SHA256] SOURCE` | Install that exact directory as unreviewed content |
| `heartwood skills remove NAME` | Remove an installed extension |

`--source` is required only when more than one signed source is configured.
`--approve` requires the complete digest printed by the matching inspect command.

## Experiment Records

| Command | Purpose |
|---|---|
| `heartwood experiments record [OPTIONS] SCRIPT [ARGUMENTS...]` | Run a declared analysis script and record its outcome |
| `heartwood experiments list [--json]` | Project-wide workflow and script records, without starting a model |
| `heartwood experiments export` | Verified experiment JSONL on standard output |
| `heartwood experiments environment [--python EXECUTABLE]` | Version metadata of a Python environment; defaults to Heartwood's interpreter |
| `heartwood experiments recover RUN_ID` | Mark an abandoned script record interrupted; does not stop processes |
| `heartwood experiments cancel RUN_ID` | Close an abandoned script record; does not stop processes or delete outputs |

`record` options come before `SCRIPT`; everything after it is passed to the script.
Repeat `--input`, `--output` (a new file), and `--code` as needed.
`--run-id` gives a stable identity that never repeats execution, `--actor` a non-sensitive actor reference, and `--runner` another interpreter such as `bash` or `Rscript`, which requires `--environment-sha256`.
Script recording is neither agent execution nor a sandbox; see [Experiment Records](../architecture/experiments.md).

## Session Automation

| Command | Purpose |
|---|---|
| `heartwood allow [ACTION_SET_ID]` | Allow the complete pending action set once; alias `approve` |
| `heartwood reject [ACTION_SET_ID]` | Reject the complete pending action set; alias `deny` |
| `heartwood pause` | Pause the selected session |
| `heartwood resume` | Resume the selected session |
| `heartwood replay` | Replay the persisted session event stream |
| `heartwood audit export [--output PATH]` | Export scrubbed audit JSONL, optionally copied to `PATH` |
| `heartwood audit verify` | Fully verify the session and audit history |
| `heartwood usage summary [--json]` | Report content-free project counts and outcomes after verifying every session's history |

Select the session with the top-level `--session-id`, for example `heartwood --session-id study-a pause`.

`heartwood usage summary` reads the records of every session in the project and the project's experiment records without adding records or contacting a model route.
It reports sessions, turns, allowed and rejected action sets, workflow runs by phase, reproduction outcomes and experiment runs by status, with the release version, the platform identifier and a random summary identifier created with the project state.
It copies no prompt, path, file name, session title, model output or record identifier.
If any session's history fails verification, it reports the failure without counts and exits with status `1`.
If a session has an interrupted commit, it reports the history as incomplete, again without counts and with status `1`, and leaves the commit for Heartwood to complete when the session is next opened or verified, for example with `heartwood audit verify`.
`--json` prints the published `heartwood.project-usage-summary.v1` document; see [Run a Pilot Evaluation](../operate/index.md#run-a-pilot-evaluation).

## Project Inspection

| Command | Purpose |
|---|---|
| `heartwood files list [--depth N] [DIRECTORY]` | Bounded project tree; default `.` |
| `heartwood files show FILE` | One bounded UTF-8 text file |
| `heartwood changes` | Changed paths: Git changes, or typed file actions of the session outside Git |
| `heartwood changes FILE` | One bounded read-only diff, or the current file outside Git |

These commands never show private project state and exit nonzero for unavailable, binary, or unsupported content.

## Operator Commands

| Command | Purpose |
|---|---|
| `heartwood runtime start [OPTIONS]` | Review and start the selected Heartwood-managed model runtime, requesting Slurm compute where required |
| `heartwood gateway serve [OPTIONS]` | Serve the gateway and packaged browser interface, without guided setup or runtime start |
| `heartwood audit signer list` | Signer profiles the deployment approves |
| `heartwood audit signer select PROFILE` | Select an approved signer profile for this project |
| `heartwood audit signer default` | Return to the deployment default signer |
| `heartwood audit checkpoint --output DIR --deployment-id ID --retention-policy ID --retain-until YYYY-MM-DD [--include-experiments]` | Create a signed audit bundle outside the project, optionally binding the project's experiment records |
| `heartwood audit verify-checkpoint [--public-key KEY] BUNDLE` | Verify a bundle with the active signer profile or an independently trusted key |
| `heartwood signer init-local [--directory DIR] [--profile ID] [--key-version V]` | Create the local fallback signer outside the project; defaults `local-development` and `v1` |
| `heartwood signer serve-local [--registry PATH] [--profile ID] [--private-key PATH]` | Run that signer on its owner-only Unix socket |

Checkpoints never replace an existing output, and private signing keys are never command arguments or project state.
See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md).

The operator deployment manifest that `gateway serve --deployment` and the migration commands read is specified in [Deployment Manifest](configuration.md#deployment-manifest).

### Runtime Start

| Option | Meaning |
|---|---|
| `--dry-run` | Print the model, resource, and scheduler plan without downloading, starting inference, or requesting compute |
| `--task-profile {auto,standard,powerful,maximum}` | Capability tier for the automatic recommendation; default `auto` |
| `--partition NAME` | Slurm GPU partition; default is the available default partition |
| `--gpus N` | Advanced: must match a qualified tensor-parallel configuration |
| `--cpus N`, `--memory SIZE`, `--time HH:MM:SS` | Allocation request; `--time` defaults to `02:00:00` |
| `--startup-timeout SECONDS` | Wait for runtime readiness; default `600` |
| `--no-allocate` | Never request scheduler compute; stop when the plan needs an allocation |
| `--yes-request-allocation` | Confirm the displayed scheduler request |
| `--yes-download` | Confirm the displayed pinned model download |

Unattended operation needs both `--yes-download` and `--yes-request-allocation`; no other flag implies either.
Top-level `--interface`, `--port`, `--plain`, `--prompt`, and `--review-mode` select the interface started afterwards.

### Gateway Serve

| Option | Meaning |
|---|---|
| `--host HOST` | Bind host; default `127.0.0.1` |
| `--port PORT` | Bind port; default `8767` |
| `--web-root DIR` | Browser asset directory; default the packaged assets |
| `--deployment PATH` | Absolute operator manifest binding the project to external state, policy, and optional managed workers |
| `--review-mode MODE` | Review mode selected at start; policy limits still apply |
| `--capability-fd N` | Read the operator capability from inherited descriptor `N` instead of printing a launch link |
| `--ingress-mode {direct-loopback,jupyter-proxy,trusted-proxy}` | Network route; default the platform's declared mode |
| `--public-origin ORIGIN` | Exact browser-visible `http` or `https` origin of a proxy route |
| `--base-path PATH` | Exact browser-visible gateway prefix; default `/` |
| `--trusted-proxy-source IP_OR_CIDR` | Trusted proxy source; repeat for several |
| `--trusted-identity-header NAME` | Header carrying a non-secret proxy identity assertion |
| `--trusted-identity VALUE` | Exact value of that assertion |
| `--proxy-strips-prefix` | The trusted proxy removes the external base path before forwarding |
| `--host-loopback-publication` | A wildcard container bind is published only on the host loopback interface |

Without `--capability-fd`, the gateway prints one launch link after it starts listening.
The page exchanges the link's fragment token for a tab-scoped capability; the link then stops working, the tab keeps access across reloads, and a gateway restart prints a new link.
With `--capability-fd N` (`N` at least 3), the gateway reads at most 4096 bytes, accepts one trailing newline, requires end of input within five seconds, closes the descriptor, and prints no link:

```bash
heartwood gateway serve --capability-fd 3 3</run/secrets/heartwood-capability
```

The secret is 32 to 4096 printable ASCII characters without spaces, quotes, commas, semicolons, or backslashes, and stays outside worker access; the proxy sends it in `X-Heartwood-Capability`.
`HEARTWOOD_GATEWAY_CAPABILITY` in the environment is rejected.

Workstations and Carina default to direct loopback and refuse a non-loopback bind; Terra defaults to Jupyter proxy mode and needs its exact public origin and base path.
`--host-loopback-publication` is valid only with a host mapping such as `-p 127.0.0.1:8767:8767` and does not inspect the image.
A trusted identity assertion is a route marker, not a credential; the proxy must strip client-supplied forwarding and identity headers.
See [Add a Platform](../operate/platform-integration.md#gateway-ingress).

With a manifest that declares a `[worker]` table, `gateway serve` first checks that the sandbox gateway reports both [deletion capabilities](../operate/platform-requirements.md#gateway-behavior-behind-native-qualification) and stops with [`HW-WORKER-001`](troubleshooting.md#hw-worker-001-sandbox-gateway-lacks-a-required-deletion-capability) when a capability is missing or the gateway's report cannot be read.
It then reconciles worker attempts that an earlier gateway process for the same deployment left unrevoked, and prints one line for each session whose revocation was not observed; that attempt stays fenced and is retried at the next start.

## Project State Migration

Run these from the research directory with an operator deployment manifest:

```bash
heartwood project migrate --deployment /absolute/path/project.toml --backup /absolute/protected/original
heartwood project recover-migration --deployment /absolute/path/project.toml
heartwood project recover-migration --deployment /absolute/path/project.toml --rollback
```

`migrate` moves local `.heartwood/` state to the manifest's `state_root` and requires stopped project activity, a new target and backup, and verified source records.
The backup parent must be on the research directory's filesystem, and both external parents must already exist and be operator-controlled.
`recover-migration` resumes an interrupted publication; `--rollback` restores the original only while the protected target is unchanged and keeps both copies.
Each command prints the path of its migration evidence.
See [Moving Existing Local State](../start/project.md#moving-existing-local-state).

## Repository Scripts

These run from a repository checkout, not an installed release.
[Testing and Evidence](../architecture/testing.md) describes when each is used.

| Command | Purpose |
|---|---|
| `uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --bootstrap` | Build or verify the maintained runtime wheels in `vendor/runtime-wheels/` |
| `… deploy/build-worker-candidate.py --output DIR` | Build the maintained SDK wheels into a new directory |
| `… deploy/build-worker-candidate.py --application --output DIR` | Build the committed Heartwood application wheels |
| `… deploy/build-worker-candidate.py --gateway --cache DIR --output DIR [--gateway-tests]` | Build the pinned qualification-only sandbox gateway; `--gateway-tests` runs its deletion regressions and needs 12 GiB of container memory |
| `python3 deploy/qualify-native-worker.py --worker-image IMAGE --runtime-image IMAGE --gateway-artifacts DIR --records-artifacts DIR --output DIR [--model-profile PATH]` | Run native worker qualification; `--model-profile` adds a capable-model case whose credential is read from standard input |
| `python3 deploy/qualification_evidence.py [--commit SHA] DIR` | List every reason an evidence directory does not establish a qualification pass |
| `uv run python packages/gateway/scripts/export_platform_contract.py DIR` | Export the platform contract bundle to a new directory |

`build-worker-candidate.py` requires uv 0.13.7; pass another executable with `--uv PATH`.

## Exit Status

`0` means the command completed, or an interactive cancellation changed no files.
Argument errors exit `2` with usage output.
Other failures print a recovery message and exit nonzero: `64` for unavailable project files, project state, or migration input, `65` for audit and checkpoint failures, `75` when another process owns the session or project, and `130` on interruption.
