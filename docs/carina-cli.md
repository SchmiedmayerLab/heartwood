<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Carina CLI Pilot

This guide defines the synthetic-only Heartwood pilot on Stanford Carina. The repository implements the platform detector, conservative local policy, setup diagnostics, locked native environments, Slurm launcher, loopback vLLM profile, and Stanford AI API Gateway connection. Live Carina execution remains required before this path is marked live-validated. Nothing in this guide authorizes protected health information or unrestricted agent tools for controlled data.

## Safety Boundary

Use a new project directory containing only the verified Heartwood installation, a reviewed public model, and synthetic fixtures. Do not point Heartwood at an existing research directory, enumerate unrelated project storage, print the full environment, or copy controlled data into job-local scratch. The first pilot uses **Ask Every Time** and one CLI process; terminal and file tools still run with the researcher's Unix permissions.

## Prepare Persistent Storage

On a login node, select protected project paths owned by the pilot:

```bash
export HEARTWOOD_ROOT=/projects/<group>/<project>/heartwood-pilot
mkdir -p "${HEARTWOOD_ROOT}/models"
```

Do not leave a GitHub token in the job environment.

## Install A Tagged Release

Load Carina's supported Micromamba module. Download the standalone installer from the selected GitHub Release, review its checksum or attestation, and run:

```bash
chmod +x heartwood-installer
./heartwood-installer --root "${HEARTWOOD_ROOT}" --platform carina --version <release-tag>
export PATH="${HEARTWOOD_ROOT}/bin:${PATH}"
```

The installer retrieves and verifies the matching native bundle, keeps the immutable source payload and runtime under versioned directories, creates separate locked Heartwood and vLLM environments, and publishes `${HEARTWOOD_ROOT}/bin/heartwood`. For restricted-network transfer, place `heartwood-native.tar.gz`, `SHA256SUMS`, and `heartwood-installer` through the approved path and use `--bundle` with `--checksums`. The installer never downloads model weights or stores credentials.

## Stage A Reviewed Model

Place the approved Hugging Face snapshot under `${HEARTWOOD_ROOT}/models/<model>` using an approved transfer path. Add a `SHA256SUMS` file containing every regular model file. The launcher rejects missing, unlisted, linked, or modified files and stages the verified snapshot into a fresh job-scratch directory. Model acquisition is deliberately separate from agent startup; the launcher never downloads weights.

## Review And Launch

Preview the complete launch plan without requesting compute:

```bash
export HEARTWOOD_PLATFORM=carina
heartwood --workspace "${HEARTWOOD_ROOT}/state/sessions" doctor
heartwood launch \
  --model-root "${HEARTWOOD_ROOT}/models/<model>" \
  --dry-run
```

Launch the session:

```bash
heartwood launch --model-root "${HEARTWOOD_ROOT}/models/<model>"
```

On a login node, Heartwood displays the `gpu` partition, GPU, CPU, memory, and time request and asks before invoking `srun`. Scheduler consent is independent of agent action confirmation. Inside the allocation, the same command verifies the model manifest, stages it into `$LOCAL_SCRATCH_JOB`, removes unrelated provider and source-control credentials, starts vLLM on `127.0.0.1:8765`, waits for `/v1/models`, configures the Local connection, and opens `heartwood chat`. vLLM stops when the CLI exits. Use `--no-allocate` to prohibit scheduler submission or `--yes-request-allocation` only in reviewed automation.

The compatibility scripts under `deploy/carina` remain available from the installed source payload for troubleshooting; they delegate to the packaged launch contract and are not the normal researcher workflow.

## Action Confirmation

Setup defaults to **Ask Every Time**. After validating the synthetic workflow, a researcher may explicitly select:

```bash
heartwood actions set auto-approve-low-risk
```

This uses OpenHands risk analysis: low-risk actions continue automatically, while medium-, high-, and unknown-risk actions still require **Allow once** or **Reject**. Heartwood does not expose unconditional auto-approval. This software option does not establish approval for controlled data.

## Stanford AI API Gateway

The managed alternative uses `https://aiapi-prod.stanford.edu/v1/models` and `https://aiapi-prod.stanford.edu/v1/chat/completions` through the OpenAI-compatible connection. Supply the individually issued key only at runtime:

```bash
export STANFORD_AI_API_KEY="<runtime-secret>"
heartwood --workspace "${HEARTWOOD_ROOT}/state/sessions" setup \
  --model-source stanford-ai-api-gateway
```

Heartwood discovers the exact aliases available to that key and never stores its value. The Stanford GenAI Evaluation Matrix, applicable agreement, Data Risk Assessment, and project authorization determine whether a particular service and model may receive a particular data classification. Model-route eligibility does not approve the OpenHands terminal or file tools.

## Validation Evidence

The synthetic acceptance records only the Heartwood revision, environment locks, model snapshot digest, Skill digests, Slurm job resources, route decision, action decisions, expected aggregate output, restart replay, and scrubbed audit export. Do not capture prompts, model responses, file listings, environment dumps, tokens, or participant-level data as evidence.

After the pilot, run `heartwood doctor`, restart the CLI with the same workspace, use `/replay`, reject one proposed action, approve the reviewed synthetic action, and export the audit record. Mark Carina live-validated only after every acceptance criterion in [Issue #25](https://github.com/SchmiedmayerLab/heartwood/issues/25) passes.
