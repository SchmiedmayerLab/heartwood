<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Set Up Heartwood On Carina

This guide installs Heartwood `0.1.1` in isolated project storage, downloads the reviewed public demonstration model, requests one Carina GPU allocation, and opens the shared Heartwood conversation. Heartwood handles the runtime dependencies, model manifest, scheduler discovery, compute-local staging, local inference startup, setup, and cleanup. Use synthetic data only. This workflow is implemented but remains pending clean published-artifact validation in [Issue #25](https://github.com/SchmiedmayerLab/heartwood/issues/25); it does not authorize protected health information or controlled-data access.

## Choose Isolated Project Storage

Use a new writable directory under an approved project allocation. Do not install the runtime or model in the small login-node home filesystem, and do not point the agent at an existing research workspace.

```bash
export HEARTWOOD_ROOT=/projects/<group>/<project>/heartwood-synthetic
mkdir -p "${HEARTWOOD_ROOT}"
chmod 700 "${HEARTWOOD_ROOT}"
```

The local demonstration requires at least 20 GiB free for the reviewed model snapshot in addition to the application environment and job-local staging capacity.

## Install A Release

Select the immutable `0.1.1` release and download its standalone installer:

```bash
export HEARTWOOD_VERSION=0.1.1
curl --fail --location --remote-name \
  "https://github.com/SchmiedmayerLab/heartwood/releases/download/${HEARTWOOD_VERSION}/heartwood-installer"
chmod +x heartwood-installer
./heartwood-installer \
  --root "${HEARTWOOD_ROOT}" \
  --platform carina \
  --version "${HEARTWOOD_VERSION}"
export PATH="${HEARTWOOD_ROOT}/bin:${PATH}"
```

Release `0.1.1` is the first release with the corrected Carina installation and launch path. Available immutable tags are listed under [Heartwood releases](https://github.com/SchmiedmayerLab/heartwood/releases), and [Issue #25](https://github.com/SchmiedmayerLab/heartwood/issues/25) tracks its clean live-platform validation.

The installer reports each stage and elapsed completion time, verifies the release bundle, loads the supported Carina Micromamba module when needed, creates the private state, model, cache, runtime, and log directories, installs a version-pinned FFmpeg bootstrap plus the locked Heartwood and hash-locked vLLM environments, and imports the real TorchCodec and vLLM modules. Dependency solving and runtime installation can take several minutes. The installer does not download a model or store credentials.

Run the read-only readiness check:

```bash
heartwood doctor
```

Before local-model setup, the login node reports `State: setup-required`. After a successful local-model launch has configured the persistent route, it reports `State: compute-required`. Both states are healthy on a login node: GPU visibility and job-local scratch are checked only after Heartwood enters an allocation. `State: recovery-required` identifies an actual configuration or in-allocation failure.

## Download The Reviewed Local Model

List reviewed local models and download the vLLM demonstration snapshot:

```bash
heartwood models artifacts
heartwood models download qwen25-7b-instruct-vllm
export HEARTWOOD_MODEL_ROOT="${HEARTWOOD_ROOT}/models/qwen25-7b-instruct-vllm"
```

Heartwood creates the destination, uses Hugging Face's snapshot downloader at the pinned repository revision, reports its native transfer progress, removes transient cache metadata, writes source provenance and an exact `SHA256SUMS` manifest, and verifies every file before publishing the directory. A public download does not require a Hugging Face token; a token may provide higher rate limits but must remain a runtime-only secret.

An administrator may instead place an approved snapshot at the same location through an authorized transfer path. It must contain an exact `SHA256SUMS` manifest before launch.

## Review And Launch

Preview the detected scheduler request without submitting it:

```bash
heartwood launch --model-root "${HEARTWOOD_MODEL_ROOT}" --dry-run
```

Heartwood discovers the available Carina GPU partitions and selects the scheduler default. An explicit `--partition` or `HEARTWOOD_SLURM_PARTITION` overrides discovery and is validated before consent.

Start the session:

```bash
heartwood launch --model-root "${HEARTWOOD_MODEL_ROOT}"
```

Review the displayed partition, GPU, CPU, memory, and time request. Heartwood asks before invoking `srun`. After allocation, a six-stage progress display verifies and stages the model in job-local scratch, validates the packaged inference runtime, starts vLLM on loopback, reports elapsed startup time and periodic waiting updates, configures the local model route, displays the managed agent workspace, and opens the terminal client. A startup failure reports the runtime log and last relevant error. vLLM and owned scratch staging stop when the session exits.

Use `--partition`, `--gpus`, `--cpus`, `--memory`, and `--time` only when the reviewed task needs a non-default request. Use `--no-allocate` to prohibit scheduler submission and `--yes-request-allocation` only in reviewed automation.

## Use The Session

See [Using Heartwood](using-heartwood.md) for terminal navigation, action-set review, line mode, replay, audit export, and the equivalent web and notebook interfaces.

## Use The Stanford AI API Gateway

The managed model alternative does not require a local model download or vLLM launch. Supply the individually issued credential only for setup and model calls:

```bash
export STANFORD_AI_API_KEY="<runtime-secret>"
heartwood setup --model-source stanford-ai-api-gateway
heartwood
```

Heartwood discovers the exact model identifiers available to the credential and does not persist its value. The applicable Stanford service agreement, GenAI Evaluation Matrix, Data Risk Assessment, project authorization, and platform controls determine whether a selected model route may receive a data classification. Route authorization does not approve agent tools or data export.

## Validate And Exit

Use only a synthetic workspace for the platform acceptance run. Approve one reviewed action set, reject a separate action set, exit, restart `heartwood` with the same state root, replay the session, and export the scrubbed audit record. Do not collect prompts, responses, broad file listings, environment dumps, credential values, or participant-level records as validation evidence.
