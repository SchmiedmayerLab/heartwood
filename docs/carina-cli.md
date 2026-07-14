<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood on Stanford Carina

This guide installs Heartwood on Carina and runs a synthetic local-model validation. Use an isolated directory containing no protected health information. Do not inspect unrelated project directories, environment contents, or cluster data as part of validation.

Heartwood uses two locations with separate purposes:

- The **installation root** holds versioned application and inference environments and may be shared by several Heartwood projects.
- The **project directory** holds the files the agent may edit and the project's private `.heartwood/` state.

## Prepare Private Storage

Choose a writable group project with sufficient quota. The reviewed GPU snapshot is approximately 15.2 GB, and Carina also needs enough job-local scratch to stage it.

```bash
INSTALL_ROOT=/projects/<group>/<user>/heartwood-installation
PROJECT=/projects/<group>/<user>/heartwood-synthetic-demo

mkdir -p -m 700 "$INSTALL_ROOT" "$PROJECT"
cd "$PROJECT"
```

Keep the project separate from the installation root. Heartwood will create `.heartwood/` in the current directory; no state or model paths need to be exported.

## Install Release 0.2.0

Load Carina's supported package manager and download the immutable installer:

```bash
module load micromamba/2.3.3
curl --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0/heartwood-installer
chmod +x heartwood-installer
./heartwood-installer \
  --root "$INSTALL_ROOT" \
  --platform carina \
  --version 0.2.0
export PATH="$INSTALL_ROOT/bin:$PATH"
```

The installer verifies the release bundle, creates versioned source and runtime environments, installs the locked Heartwood application, installs the hash-locked vLLM environment, and validates FFmpeg, TorchCodec, and vLLM imports. It does not create project state, download a model, or store a credential.

Confirm the installation and project readiness:

```bash
heartwood --version
heartwood doctor
```

Before setup, `heartwood doctor` reports `setup-required`. This is expected.

## Download the Reviewed GPU Model

From the synthetic project directory:

```bash
heartwood models artifacts
heartwood models download qwen25-7b-instruct-vllm
```

Heartwood downloads the pinned Hugging Face snapshot into `.heartwood/models/`, displays transfer progress, removes transient transfer metadata, writes provenance and an exact `SHA256SUMS`, verifies every file, and saves the selected model in `.heartwood/config.toml`.

Review the project-local selection without requesting compute:

```bash
heartwood launch --dry-run
```

The plan shows the project, model, vLLM runtime, detected GPU partition, requested GPU, CPU, memory, and time. It contains no credential or manually supplied storage path.

## Launch the Session

Start Heartwood from the same project directory:

```bash
heartwood launch
```

Heartwood discovers Carina GPU partitions and prefers the scheduler default. Review the displayed request and answer `y` only when it is appropriate. If no default is available, pass one of the displayed names with `--partition`.

After allocation, Heartwood:

1. verifies the selected snapshot;
2. validates the packaged vLLM environment;
3. checks job-local scratch capacity and stages the model there;
4. starts vLLM on loopback and reports elapsed startup time while waiting;
5. validates the shared model, policy, and action settings;
6. opens the interactive terminal session.

First model startup can take several minutes. If startup fails, Heartwood prints the relevant tail of `.heartwood/logs/local-model.log` and exits the allocation cleanly. The vLLM process and staged scratch copy are removed when the session ends.

Use `--gpus`, `--cpus`, `--memory`, and `--time` only for a reviewed task that needs different resources. `--no-allocate` prohibits scheduler submission, and `--yes-request-allocation` is reserved for reviewed automation.

## Run a Synthetic Action Check

Ask for one bounded action in the project, inspect the complete action set, and allow it once. Then ask for a separate action and reject it. For example:

```text
Create carina-smoke.txt in this project containing exactly heartwood-carina-synthetic-ok followed by one newline. Propose one file create action and stop.
```

Use `/replay` to confirm that the proposal, grouped decision, tool result, and agent response persist. Export the scrubbed record with `/audit-export`. Do not collect prompt text, model output, broad file listings, environment dumps, credentials, or participant-level records as external validation evidence.

After exiting, run:

```bash
heartwood doctor
```

A configured local model reports `compute-required` on the login node because inference needs a Slurm allocation. This is healthy. `recovery-required` identifies an actual project configuration or in-allocation failure.

## Use the Stanford AI API Gateway

The managed route does not require a local model download or GPU allocation. Start Heartwood from the project, choose **Stanford AI API Gateway**, enter the individually issued token at the hidden prompt, and select one of the aliases returned by the service:

```bash
heartwood
```

Heartwood stores only the selected alias and a non-secret credential binding. A token entered at the prompt remains in the running process and must be entered again after restart unless Carina supplies an approved platform secret binding. It is not written to `.heartwood/`, command arguments, logs, events, or audit exports.

The Stanford service agreement, GenAI Evaluation Matrix, Data Risk Assessment, project authorization, and Carina controls determine whether a route may receive a particular data classification. A successful connection does not authorize agent tools or data export.

See [Use Heartwood](using-heartwood.md) for terminal controls and [Platform Support](platform-support.md) for the distinction between CI validation, live validation, and institutional approval.
