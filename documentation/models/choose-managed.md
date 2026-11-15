<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose a Heartwood-Managed Model

A Heartwood-managed model keeps inference on the compute where Heartwood runs.
Model files are downloaded separately into the project's `.heartwood/models/`; images and installers contain no weights.
Heartwood-managed models serve local launches; an isolated worker uses the model endpoint its operator configures.

Start the guided selection in either interface:

- run `heartwood` and choose **Run with Heartwood**; or
- open **Settings → Models** in the browser.

Both read the same catalog and project state, and show the download size, resource guidance, license and immutable source revision before asking for confirmation.

## Capability Tiers

The tier describes the intended agent workload, not scientific quality.

| Tier | Intended Use |
|---|---|
| Standard | Bounded edits, focused analysis scripts and first-time use |
| Powerful | Larger repositories, multi-step coding tasks and longer sessions |
| Maximum capability | Broad multi-file work on substantial multi-GPU compute |

The guided selector recommends only configurations that fit the detected environment and have completed Heartwood's coding-agent qualification on that platform.
Other catalog entries are labelled **Not tested** in the terminal and listed under **Models not qualified for this platform** in the browser; they can be selected explicitly for evaluation but are never recommended.

List the complete catalog for the current environment:

```bash
heartwood models managed
```

Precision, tool parser, context, tensor parallelism and pinned revision are shown in the detailed model view and in the [GPU compatibility matrix](../reference/gpu-compatibility.md).

## Resource Guide

| Tier | Model Configuration | Qualified On | Download | GPU Memory | Recommended RAM | Recommended Free Disk | Default Context | Estimated Runtime Startup |
|---|---|---|---:|---:|---:|---:|---:|---:|
| Standard | Qwen2.5 7B Instruct Q4_K_M, CPU | Generic platform (containers) | 4.36 GiB | None | 32 GB | 50 GB | 32,768 | Hardware dependent |
| Powerful | Qwen3 Coder 30B W4A16 AWQ | Terra, 2 x T4 | 16.81 GiB | 2 x 16 GB | 120 GB | 50 GB | 18,432 | 2-15 minutes |
| Maximum capability | Muse Glimmer 30B BF16 | Carina, 2 x L40S | 55.49 GiB | 2 x 48 GB | 128 GB | 96 GB | 32,768 | 5-20 minutes |

These values are planning estimates; the selection screen reports the exact values from the release catalog before making changes.
Startup time is measured once the model is in the project cache, and later starts reuse the verified snapshot.
A first download depends on network conditions and Hugging Face rate limits.

Weights are only part of the memory requirement: the runtime also needs room for temporary downloads, the key/value cache and request handling.
Heartwood keeps conservative headroom and can choose a smaller context than the model's advertised maximum.
A qualification covers its exact platform and GPU count; for example, one-T4 and four-T4 Terra results do not qualify the two-T4 configuration or each other.

## Other Hugging Face Models

Choose **Other Hugging Face model** in the terminal or **Other model** in the browser, or use the command line:

```bash
heartwood models inspect unsloth/Qwen2.5-Coder-7B-Instruct-GGUF
heartwood models download unsloth/Qwen2.5-Coder-7B-Instruct-GGUF
```

Heartwood queries Hugging Face, resolves the requested tag or branch to an immutable commit, and selects a supported artifact: a single-file GGUF for the llama.cpp CPU runtime or a standard safetensors snapshot for the NVIDIA vLLM runtime.
No download begins until you approve the displayed plan.
Downloading a GPU model needs the vLLM runtime and enough project storage, but no active GPU allocation; GPU count and memory are checked when the runtime starts.

To request a particular tag, branch or commit, pass `--revision REVISION` in the terminal or expand **Version options** in the browser.

Large public snapshots can hit Hugging Face's anonymous download limits.
When an approved Hugging Face token is available, expose `HF_TOKEN` only to the download process, never in project configuration.
If a transfer stops, rerun the same command from the project directory; Heartwood keeps completed files in a private partial snapshot for the same immutable revision and publishes the model into `.heartwood/models/` only after full verification.

The planner stops with a message that Heartwood does not yet support the repository when a repository requires executable remote code, has an unsupported weight layout or architecture, lacks a resolvable revision, or cannot fit the available runtime.
Report a compatible-looking rejection through the [Heartwood issue form](https://github.com/SchmiedmayerLab/heartwood/issues/new/choose) instead of bypassing the check.

## Before You Approve a Download

Check these items in the displayed plan:

- tool use: a coding agent needs reliable structured tool calls, not only code completion;
- license and provenance: confirm the license and keep the repository and immutable revision;
- compute: GPU count, GPU memory, RAM, disk and expected allocation cost;
- context: larger windows use substantially more memory and can increase latency; and
- data policy: running in the same environment does not approve the model or platform for controlled data.

Parameter count alone does not predict agent quality or resource use.

## Import Existing Model Files

Use a [Heartwood model bundle](offline.md) when another Heartwood project prepared the model for an offline destination; it carries the file list, runtime settings and digests.
Use a raw import only for reviewed model files without a bundle:

```bash
heartwood models import /approved/path/model.gguf \
  --source owner/model \
  --revision 0123456789abcdef0123456789abcdef01234567 \
  --license apache-2.0
```

Heartwood accepts a valid GGUF file or a standard vLLM safetensors directory, rejects symbolic links and executable Python, records provenance, and copies the artifact atomically into `.heartwood/models/`.
The path must be visible to the Heartwood process; the browser uses the same server-side import and does not upload model files through the page.
The destination needs free space for a complete copy plus a 512 MiB reserve, and a directory import creates and verifies a SHA-256 manifest before the model can be selected.
Keep the command or browser page open while Heartwood reports that a large import is still running.
