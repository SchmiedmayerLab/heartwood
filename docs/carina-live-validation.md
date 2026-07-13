<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Carina Release 0.1.0 Validation Findings

This record captures the synthetic-only validation of the published Heartwood `0.1.0` native artifact on Stanford Carina. It preserves the observed evidence and required follow-up without treating diagnostic workarounds as supported installation instructions. No protected health information, existing research workspace, unrelated project path, credential value, or participant-level record was used.

## Validation Status

The release is **partially live-exercised** and is not live-validated on Carina. Installation, artifact verification, model transfer, model verification, GPU allocation, local model startup after manual runtime workarounds, platform-policy correction, and a genuine OpenHands tool proposal succeeded. The end-to-end action workflow did not complete because the CLI exposed the wrong confirmation identifier and then rejected a valid multi-action OpenHands confirmation step before execution.

## Validated Evidence

- The standalone installer and native bundle checksums passed for release `0.1.0`.
- The installed commands reported Heartwood `0.1.0` and vLLM `0.25.0`.
- Carina platform detection selected the Carina launch path and required explicit consent before requesting Slurm compute.
- A new isolated project directory provided sufficient persistent capacity after the initial home-directory location proved too small.
- The public `Qwen/Qwen2.5-7B-Instruct` snapshot at revision `a09a35458c702b33eeacc393d103063234e8bc28` downloaded outside the Heartwood artifact, received a complete checksum manifest, and passed Heartwood model verification.
- A `dev` partition allocation provided an NVIDIA L40S GPU.
- Installing FFmpeg into an isolated Micromamba prefix allowed TorchCodec `0.14.0+cu130` and vLLM `0.25.0` to import.
- Disabling the FlashInfer sampler through a temporary wrapper allowed vLLM to initialize and expose the loopback OpenAI-compatible endpoint.
- After correcting persisted setup metadata, setup, policy, model source, connection, and action-confirmation mode agreed on the Carina profile.
- A real local-model turn reached OpenHands and proposed two file-editor actions confined to the Heartwood-managed synthetic session workspace.
- Neither proposed file action executed after the failed approval attempts.

## Findings

| ID | Area | Observation | Required outcome |
|---|---|---|---|
| CARINA-001 | Storage | The first isolated location had approximately 12.9 GiB available and could not hold the runtime and 15.2 GB model snapshot; authorized project storage had approximately 1 TiB available. | `setup` and `doctor` must report capacity requirements and fail before installation or download when the selected roots are insufficient. |
| CARINA-002 | Installation | Micromamba dependency resolution and installation had long periods without Heartwood stage or elapsed-time feedback. | Installation must report named stages, elapsed time, destinations, and whether continued waiting is expected. |
| CARINA-003 | Directory setup | Model setup failed because the parent `${HEARTWOOD_ROOT}/models` directory did not exist. | Installation or model setup must create owned state, cache, model, runtime, and log roots with restrictive permissions before use. |
| CARINA-004 | Model transfer | Hugging Face reported unauthenticated requests and repeated short waits for a `.gitignore.lock`; the transfer ultimately completed. | Model acquisition documentation and progress output must distinguish harmless lock waits and rate-limit guidance from failures, without requiring a token for a public snapshot. |
| CARINA-005 | Login-node diagnostics | `heartwood doctor` reported `State: recovery-required`, no job scratch, and no GPU on the login node even though this is the expected pre-allocation state. | Diagnostics must distinguish expected login-node readiness from broken configuration and present the next valid launch action. |
| CARINA-006 | Scheduler defaults | The generated request used `--partition=gpu`, but the live partitions were `dev`, `normal`, and `long`; Slurm returned `invalid partition specified: gpu`. | The Carina launch provider must discover or accept deployment-configured partitions, validate a requested partition before consent, and avoid a stale hard-coded default. |
| CARINA-007 | Runtime dependency | vLLM import failed through TorchCodec because no supported FFmpeg shared libraries were available. | The native GPU runtime and NVIDIA images must include and validate the pinned FFmpeg and TorchCodec dependency set. |
| CARINA-008 | Runtime preflight | Running `vllm --help` on the login node failed with `Failed to infer device type` despite successful Python imports. | Preflight must separate login-node package validation from allocation-side accelerator and server startup checks. |
| CARINA-009 | GPU startup | vLLM reached model loading and warm-up, then FlashInfer JIT failed because `nvcc` and `/usr/local/cuda` were unavailable. | The packaged runtime must select a supported no-JIT sampler when the CUDA toolkit is absent or package a tested toolkit path; this choice must not require a user-created executable wrapper. |
| CARINA-010 | Environment propagation | The post-allocation setup persisted `Platform: generic` because the launch subprocess removed Carina and Slurm evidence before invoking setup. | Launch must pass typed platform and allocation context to setup while continuing to remove credentials and unrelated environment values from tool subprocesses. |
| CARINA-011 | Failure reporting | Startup reported only `vLLM did not become ready; inspect .../vllm.log`, requiring manual log movement, redaction, and tailing to find the root cause. | Runtime supervision must report the failed stage, process exit status, concise sanitized root cause, log location, and next diagnostic action. |
| CARINA-012 | Runtime progress | Model verification, scratch staging, vLLM loading, graph capture, warm-up, and readiness involved material waits without a stable progress view. | Launch must expose stage transitions and periodic progress without forwarding noisy provider logs into the conversation. |
| CARINA-013 | Workspace clarity | The requested shell working directory differed from the Heartwood-managed session workspace shown in the proposed file paths. | The CLI must display the active agent workspace before a task and clearly distinguish it from the shell launch directory and persistent state root. |
| CARINA-014 | Confirmation identifier | The transcript displayed the request identifier first, but `/allow` accepted only the parenthesized tool-call identifier. Using the displayed identifier returned `no matching pending action`. | Normal approval must not require internal identifiers. The CLI and web UI must submit typed confirmation targets and expose identifiers only in details or audit views. |
| CARINA-015 | Multi-action confirmation | OpenHands proposed a create action and a view action in one confirmation stop. Heartwood accepted one identifier, then denied both actions and returned `OpenHands proposed multiple actions in one confirmation step; all were rejected before execution`. | Heartwood must support sequential review of every action in an OpenHands pending set without implicitly approving another action or making a normal multi-tool turn impossible. Execution may begin only after the required decisions are complete. |
| CARINA-016 | Terminal transcript | The plain client repeated the researcher's just-entered message, exposed event sequence numbers and route internals, duplicated denial lines, and provided no clear visual hierarchy. | The live client must render a concise conversation view while retaining the complete event stream for replay and audit. Routine allowed-route events and the just-entered line must not be echoed as new conversational content. |
| CARINA-017 | Interactive review | Approval required copying opaque identifiers into slash commands; proposed actions were not presented as navigable review steps. | The Textual client must provide keyboard-driven action review with visible tool, scope, risk, progress through pending actions, **Allow once**, **Reject**, and optional details. Arrow keys and explicit focus state must work over SSH; slash commands remain the line-mode and automation fallback. |
| CARINA-018 | Status presentation | Some status output was visually clipped or appeared incomplete in the interactive terminal, making it unclear whether policy and model state were fully reported. | Terminal tests must cover narrow SSH dimensions, wrapping, resize, long policy reasons, and line-mode output without truncating state. |
| CARINA-019 | Continuous integration | Existing native checks verified package metadata but did not import TorchCodec, start vLLM on a native GPU, exercise sampler warm-up, preserve Carina setup context, or complete a multi-action confirmation flow. | Continuous integration must add dependency-import, native-GPU startup, setup-context, progress-state, sequential-confirmation, narrow-terminal, and end-to-end synthetic action gates. |

## Observed Failure Signatures

The following sanitized messages identify the failure classes that regression tests must cover:

```text
mkdir: cannot create directory '<HEARTWOOD_ROOT>/models/<model>': No such file or directory
```

```text
srun: error: invalid partition specified: gpu
srun: error: Unable to allocate resources: Invalid partition name specified
```

```text
vLLM did not become ready; inspect <HEARTWOOD_ROOT>/state/runtime/vllm.log
```

```text
RuntimeError: Could not load libtorchcodec.
OSError: libavutil.so.60: cannot open shared object file: No such file or directory
```

```text
Lmod has detected the following error: Unable to find: "ffmpeg".
```

```text
RuntimeError: Failed to infer device type
```

```text
RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist
RuntimeError: Engine core initialization failed. See root cause above.
```

```text
Error: no matching pending action: <confirmation-request-id>
```

```text
Action denied
Action denied
Error: OpenHands proposed multiple actions in one confirmation step; all were rejected before execution
```

## Diagnostic Workarounds Used

The pilot installed FFmpeg `8.1.2` in a separate Micromamba prefix, added its `bin` and `lib` directories to the runtime environment, and launched vLLM through a wrapper that set `VLLM_USE_FLASHINFER_SAMPLER=0`. It also corrected the persisted deployment profile through the Heartwood Python API and supplied `--partition dev` explicitly. These steps proved the likely fixes but are not an acceptable researcher workflow and must not become prerequisites in the operational guide.

## Required Implementation Passes

### Pass 1 — Native Runtime Completeness

- Package and lock the complete vLLM, PyTorch, TorchCodec, FFmpeg, CUDA-driver compatibility, and sampler configuration.
- Add login-node import diagnostics and allocation-side server startup diagnostics with different readiness semantics.
- Verify the same dependency contract in the native bundle and generic and Terra NVIDIA images.

### Pass 2 — Carina Setup And Launch

- Discover or configure valid partitions and validate the complete Slurm request before asking for consent.
- Preserve typed Carina and allocation context across launch, setup, doctor, and conversation processes.
- Create required roots, check capacity, verify writable persistent storage and job scratch, and display the managed agent workspace.

### Pass 3 — Progress And Recovery

- Add stable stages for installation, model verification, scratch staging, runtime import, model loading, warm-up, endpoint readiness, setup, and conversation startup.
- Emit periodic elapsed-time updates for long stages and surface concise sanitized root causes with actionable recovery commands.
- Keep detailed logs available without requiring users to discover, move, redact, or parse them manually.

### Pass 4 — Confirmation Contract

- Model one OpenHands pending set explicitly and present each action for a separate decision.
- Permit `/allow` and `/reject` without identifiers for the currently displayed action while retaining explicit identifiers for automation and audit correlation.
- Do not execute any unreviewed action, do not implicitly approve sibling actions, and do not reject a valid set solely because it contains more than one action.
- Define restart behavior for partially reviewed sets and record every decision and execution outcome unambiguously.

### Pass 5 — Interactive Terminal Experience

- Make the full-screen Textual client the normal capable-terminal experience on Carina and retain line mode for limited terminals and scripts.
- Add keyboard-navigable confirmation controls, color with non-color equivalents, visible focus, action detail expansion, pending-action progress, wrapped output, and clear working and waiting states.
- Separate concise live presentation from complete replay and audit projections; do not repeat the user's submitted line or routine allowed-route details in the live response.

### Pass 6 — Acceptance And Regression Coverage

- Add deterministic tests for sequential multi-action review, rejection, restart, route reauthorization, and audit reconstruction.
- Add headless Textual tests for arrow-key review, narrow SSH terminals, resize, focus, color-independent status, and slash-command parity.
- Add a native NVIDIA gate that imports the resolved runtime, starts vLLM, completes warm-up without a compiler dependency, serves the pinned test model, and completes a real OpenHands file or terminal action.
- Repeat the published-artifact Carina pilot from a clean project root and complete approve, reject, execution, replay, restart, and scrubbed audit export before changing the platform status to live-validated.

## Completion Gate

Carina remains not live-validated until an unmodified published artifact completes the synthetic workflow without manually installing runtime dependencies, creating wrappers, editing persisted settings through Python, discovering an undocumented partition override, or entering internal confirmation identifiers. The final evidence must include successful local-model startup, a genuine model response, separately reviewed actions, at least one executed action, at least one rejected action, restart replay, and a scrubbed audit export.
