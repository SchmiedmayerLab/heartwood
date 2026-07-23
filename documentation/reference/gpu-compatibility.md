<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# GPU Compatibility

Heartwood keeps the NVIDIA inference runtime and its model configurations in a release-owned compatibility matrix.
The matrix records platform-specific outcomes as Qualified, Inconclusive, or Unsupported.
Heartwood recommends and automatically selects only Qualified configurations.

## Runtime

| Component | Locked Version |
|---|---|
| Python | 3.12 |
| vLLM | `0.25.1+cu129` |
| PyTorch | `2.11.0+cu129` |
| TorchAudio | `2.11.0+cu129` |
| TorchVision | `0.26.0+cu129` |
| CUDA application binary interface | 12.9 |
| Minimum NVIDIA Linux driver | `525.60.13` |

The vLLM environment is installed separately from Heartwood's application environment and resolved from a fully hashed lock.
Its dependency exclusions prevent a package resolver from replacing the CUDA 12.9 stack with CUDA 13 artifacts.
CUDA 13 is not qualified for Heartwood.

The minimum driver is CUDA's compatibility floor, not evidence that every driver at or above that version has completed a Heartwood qualification.
The exact driver used in a live qualification is recorded with its machine-readable result.

## Qualified Model Configurations

| Platform | Capability Tier | GPU | Model and Immutable Revision | Precision | Context | Execution | Tensor Parallelism | Server Tool Parser | Agent Tool Mode | Outcome | Date |
|---|---|---|---|---|---:|---|---:|---|---|---|---|
| Terra | Powerful | 2 x T4, 16 GB each | [Qwen3-Coder-30B-A3B-Instruct-W4A16-mixed-AWQ](https://huggingface.co/YCWTG/Qwen3-Coder-30B-A3B-Instruct-W4A16-mixed-AWQ/tree/e69e73813144d9b715648d8384b3f2c035397411) | W4A16 AWQ | 18,432 | Eager | 2 | `qwen3_coder` | OpenHands native tools | Qualified | 2026-07-22 |
| Carina | Powerful | 1 x L40S, 48 GB | [Qwen3-Coder-30B-A3B-Instruct-FP8](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8/tree/dcaee4d4dfc5ee71ad501f01f530e5652438fde0) | FP8 | 32,768 | CUDA graphs | 1 | `qwen3_coder` | OpenHands native tools | Qualified | 2026-07-21 |

All listed model repositories declare the Apache-2.0 license at the pinned revision.
Confirm that a model's license and intended use remain suitable for the project before downloading it.

## Unsupported Configurations

| Platform | Configuration | Date | Result |
|---|---|---|---|
| Terra, 1 x T4 | Qwen2.5 Coder 7B AWQ | 2026-07-21 | Unsupported: direct inference worked, but the required OpenHands tool-use workflow did not pass. |
| Terra, 1 x T4 | Qwen2.5 Coder 14B AWQ | 2026-07-21 | Unsupported: direct inference worked, but the required OpenHands tool-use workflow did not pass. |
| Terra, 4 x T4 | Qwen3 Coder 30B FP8 | 2026-07-21 | Unsupported: the FP8 Mixture-of-Experts kernel cannot load this model's quantization dimensions on T4 hardware. |
| Terra, 4 x T4 | GPT-OSS 20B MXFP4 | 2026-07-21 | Unsupported: vLLM requires compute capability 8.0 or newer, while T4 provides 7.5. |
| Terra, 4 x T4 | GPT-OSS 120B MXFP4 | 2026-07-21 | Unsupported: the same MXFP4 runtime requires compute capability 8.0 or newer, while T4 provides 7.5. |
| Terra, 4 x T4 | Qwen3 Coder 30B W4A16 AWQ with tensor parallelism 4 | 2026-07-21 | Unsupported: the quantization group size crosses four-way tensor shards. Use the qualified two-GPU configuration. |

Unsupported configurations are retained only as compatibility evidence.
They are not model choices and cannot be recommended or downloaded from the managed catalog.

## Inconclusive Attempts

| Platform | Configuration | Date | Result |
|---|---|---|---|
| Carina, 2 x L40S | GPT-OSS 120B MXFP4 | 2026-07-22 | The download was interrupted and the allocation attempt stopped before model startup because the platform detector reported no compatible two-GPU capacity. |
| Carina, 2 x L40S | Qwen3 Coder Next FP8 | 2026-07-22 | vLLM reached distributed NCCL initialization but did not become ready or produce coding-agent qualification evidence. |

Inconclusive does not mean the model is incompatible.
It means the exact attempt did not produce enough evidence to qualify or reject the configuration.

The two-way Qwen3 Coder 30B AWQ configuration is qualified.
Its context is capped at 18,432 because a 32,768-token key/value cache leaves no cache blocks on two 16 GB T4 GPUs at the validated memory ceiling.

## Qualification Requirement

A configuration becomes **qualified** only after the exact model revision and locked runtime complete one bounded Heartwood task on the named platform.
The acceptance test must establish all of the following:

1. the model loads and returns a direct inference response;
2. OpenHands uses the catalog-qualified tool mode: native structured tools for supported parsers or its prompt-conversion path for models that do not reliably emit native calls;
3. Heartwood presents the complete action set for approval;
4. approval executes the proposed operation and modifies only the synthetic project;
5. a second proposed action set is rejected and does not modify the project;
6. an independent check verifies the exact file bytes;
7. a fresh process replays both decisions and the approved result; and
8. audit export validates event coverage, hash-chain integrity, and content scrubbing.

The result records the GPU model, count, memory, driver, runtime versions, model revision, context size, tensor parallelism, server parser, and agent tool mode.
Not-tested configurations are not added to the managed catalog.
Unsupported and inconclusive attempts remain only in this evidence record so Heartwood does not offer or automatically retry them.

## Unsupported Hardware

The CUDA 12.9 runtime requires an NVIDIA GPU with compute capability 7.5 or newer.
Heartwood therefore stops before model startup on P4, P100, and V100 GPUs.
Choose a T4 or newer GPU, use a hosted model route, or select the portable CPU runtime instead.

Use `heartwood doctor` for the environment summary and `heartwood runtime start --dry-run` for the complete model and allocation plan.
Do not bypass a compatibility failure by changing vLLM, PyTorch, CUDA, the model revision, tensor parallelism, or parser inside a released environment; that creates a custom configuration without qualification evidence.
