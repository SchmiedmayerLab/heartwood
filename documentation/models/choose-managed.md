<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose a Heartwood-Managed Model

Heartwood-managed inference keeps model requests in the environment where Heartwood is running.
The model files are downloaded separately into the current project's `.heartwood/models/` directory; Heartwood images and installers do not contain model weights.

Start the guided selection from either interface:

- run `heartwood` in the terminal and choose **Run with Heartwood**; or
- open **Models** in the Heartwood browser interface.

Both interfaces read the same catalog and project state.
They show the download size, resource guidance, license, and immutable source revision before asking for confirmation.

## Capability Tiers

Heartwood uses three simple tiers.
The tier describes the intended agent workload, not scientific quality.

| Tier | Intended Use |
|---|---|
| **Standard** | Bounded edits, focused analysis scripts, and first-time use |
| **Powerful** | Larger repositories, multi-step coding tasks, and longer sessions |
| **Maximum capability** | Broad multi-file work on substantial multi-GPU compute |

Within each tier, Heartwood automatically recommends only configurations that fit the detected environment and have completed the full coding-agent qualification.
Models still under evaluation are labeled **Evaluation candidate** and are never selected automatically.

List the complete catalog from the terminal:

```bash
heartwood models managed
```

Technical fields such as precision, parser, context, tensor parallelism, and pinned revision remain available in the detailed model view and the [GPU compatibility matrix](../reference/gpu-compatibility.md).

## Resource Guide

Download and startup values below are planning estimates.
The selection screen uses the release catalog as its authoritative source and reports the exact values before making changes.

| Tier | Model Configuration | Download | GPU Memory | Recommended RAM | Recommended Free Disk | Default Context | Estimated First Start |
|---|---|---:|---:|---:|---:|---:|---:|
| Standard fallback | Qwen2.5 7B Instruct Q4_K_M, CPU | 4.36 GiB | None | 32 GiB | 50 GiB | 32,768 | Hardware dependent |
| Standard candidate | Qwen2.5 Coder 7B AWQ | 5.20 GiB | 1 x 16 GB | 32 GiB | 16 GiB | 32,768 | 2-8 minutes |
| Powerful candidate | Qwen3 Coder 30B FP8 | 29.06 GiB | 1 x 48 GB | 96 GiB | 64 GiB | 32,768 | 3-10 minutes |
| Powerful candidate | Qwen3 Coder 30B BF16 | 56.88 GiB | 2 x 48 GB | 128 GiB | 96 GiB | 65,536 | 4-12 minutes |
| Maximum candidate | Qwen3 Coder Next FP8 | 74.88 GiB | 4 x 48 GB | 192 GiB | 128 GiB | 65,536 | 5-15 minutes |
| Maximum alternative candidate | GPT-OSS 120B MXFP4 | 60.79 GiB | 2 x 48 GB | 160 GiB | 112 GiB | 65,536 | 5-15 minutes |

Model weights are only part of the memory requirement.
The runtime also needs space for temporary downloads, key/value cache, request handling, and the project itself.
Heartwood therefore uses conservative headroom and may choose a smaller context than the model's advertised maximum.

## Other Hugging Face Models

Choose **Other Hugging Face model** or enter an `owner/model` identifier:

```bash
heartwood models inspect unsloth/Qwen2.5-Coder-7B-Instruct-GGUF
heartwood models download unsloth/Qwen2.5-Coder-7B-Instruct-GGUF
```

Heartwood queries Hugging Face, resolves the requested tag or branch to an immutable commit, and selects a supported artifact.
It prefers a balanced single-file GGUF for the portable llama.cpp CPU runtime or a standard safetensors snapshot for the NVIDIA vLLM runtime.
No download begins until the plan is displayed and approved.

In the browser, expand **Advanced options** to request a particular tag, branch, or commit.
In the terminal, pass `--revision REVISION` to `models inspect` or `models download`.

The planner stops with a **not yet supported** message when a repository requires executable remote code, has an unsupported weight layout or architecture, lacks a resolvable revision, or cannot fit the available runtime.
Report a compatible-looking rejection through the [Heartwood issue form](https://github.com/SchmiedmayerLab/heartwood/issues/new/choose) rather than bypassing the check.

## Before You Approve a Download

Review these items in the displayed plan:

- **Tool use:** a coding agent needs reliable structured tool calls, not only code completion.
- **License and provenance:** confirm the license and retain the repository plus immutable revision.
- **Compute:** check GPU count, GPU memory, RAM, disk, and expected allocation cost.
- **Context:** larger windows consume substantially more memory and can increase latency.
- **Data policy:** running in the same environment does not by itself approve the model or platform for controlled data.

Parameter count alone does not predict agent quality or resource use.
Architecture, quantization, context, concurrency, runtime, and tool parser all matter.

## Import Existing Model Files

For model files transferred through an approved process:

```bash
heartwood models import /approved/path/model.gguf \
  --source owner/model \
  --revision 0123456789abcdef0123456789abcdef01234567 \
  --license apache-2.0
```

Heartwood accepts a valid GGUF file or standard vLLM safetensors directory, rejects symbolic links and executable Python, records provenance, and copies the artifact atomically into `.heartwood/models/`.
The path must be visible to the Heartwood process.
The browser uses this same server-side import and does not upload multi-gigabyte model files through the page.
