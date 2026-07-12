<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Carina CLI Pilot

This guide defines the synthetic-only Heartwood pilot on Stanford Carina. The repository implements the platform detector, conservative local policy, setup diagnostics, locked native environments, Slurm launcher, loopback vLLM profile, and Stanford AI API Gateway connection. Live Carina execution remains required before this path is marked live-validated. Nothing in this guide authorizes protected health information or unrestricted agent tools for controlled data.

## Safety Boundary

Use a new project directory containing only the public Heartwood repository, a reviewed public model, and synthetic fixtures. Do not point Heartwood at an existing research directory, enumerate unrelated project storage, print the full environment, or copy controlled data into job-local scratch. The first pilot uses **Ask Every Time** and one CLI process; terminal and file tools still run with the researcher's Unix permissions.

## Prepare Persistent Storage

On a login node, select protected project paths owned by the pilot:

```bash
export HEARTWOOD_ROOT=/projects/<group>/<project>/heartwood-pilot
mkdir -p "${HEARTWOOD_ROOT}/environments" "${HEARTWOOD_ROOT}/models" "${HEARTWOOD_ROOT}/state"
```

Clone the public repository over HTTPS. Carina does not support GitHub SSH access. Do not leave a GitHub token in the job environment.

## Install The Native Environments

Load Carina's supported Micromamba module, enter the repository, and run:

```bash
deploy/carina/bootstrap.sh --environment-root "${HEARTWOOD_ROOT}/environments"
```

The bootstrap creates separate locked Heartwood and vLLM environments. This prevents the NVIDIA inference dependency set from changing the OpenHands application environment.

## Stage A Reviewed Model

Place the approved Hugging Face snapshot under `${HEARTWOOD_ROOT}/models/<model>` using an approved transfer path. Add a `SHA256SUMS` file containing every regular model file. The launcher rejects missing, unlisted, linked, or modified files and stages the verified snapshot into a fresh job-scratch directory. Model acquisition is deliberately separate from agent startup; the launcher never downloads weights.

## Start An Interactive Allocation

```bash
srun --pty --partition=dev --gres=gpu:1 --cpus-per-task=8 --mem=64G --time=02:00:00 bash
```

Inside the allocation, confirm the detected state without changing it:

```bash
export HEARTWOOD_PLATFORM=carina
export PATH="${HEARTWOOD_ROOT}/environments/heartwood/bin:${PATH}"
heartwood --workspace "${HEARTWOOD_ROOT}/state/sessions" doctor
```

Start verified local inference and the CLI:

```bash
deploy/carina/launch-interactive.sh \
  --environment-root "${HEARTWOOD_ROOT}/environments" \
  --model-root "${HEARTWOOD_ROOT}/models/<model>" \
  --state-root "${HEARTWOOD_ROOT}/state"
```

The launcher requires an active allocation and job-local scratch, verifies the model manifest, stages it into `$LOCAL_SCRATCH_JOB`, removes unrelated provider and source-control credentials, starts vLLM on `127.0.0.1:8765`, waits for `/v1/models`, configures the Local connection, and opens `heartwood chat`. vLLM stops when the CLI exits.

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
