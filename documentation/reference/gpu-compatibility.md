<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# GPU Compatibility

Heartwood keeps the NVIDIA inference runtime and its model configurations in a release-owned compatibility matrix.
The matrix records platform-specific outcomes as Qualified, Historical, Inconclusive, or Unsupported.
Heartwood recommends and automatically selects only Qualified configurations.

## Runtime

| Component | Locked Version |
|---|---|
| Python | 3.12 |
| vLLM | `0.27.2rc1.dev77+gac7509e2b.cu129` from immutable commit `ac7509e2b1db40fec2f03dde1ed4e9dfdc2338c9` |
| PyTorch | `2.13.0+cu129` |
| TorchAudio | `2.11.0+cu129` |
| TorchVision | `0.28.0+cu129` |
| CUDA application binary interface | 12.9 |
| Minimum NVIDIA Linux driver | `525.60.13` |

The vLLM environment is installed separately from Heartwood's application environment and resolved from a fully hashed lock.
The vLLM build is the official CUDA 12.9 per-commit wheel immediately following the merged upstream Muse Glimmer implementation; the intervening commit changes only the RISC-V CPU build.
Heartwood can move back to a stable release after that implementation is included and the replacement runtime passes the same qualification checks.
Its dependency exclusions prevent a package resolver from replacing the CUDA 12.9 stack with CUDA 13 artifacts.
CUDA 13 is not qualified for Heartwood.

The minimum driver is CUDA's compatibility floor, not evidence that every driver at or above that version has completed a Heartwood qualification.
The exact driver used in a live qualification is recorded with its machine-readable result.

## Qualified Model Configurations

| Platform | Capability Tier | GPU | Model and Immutable Revision | Precision | Context | Execution | Tensor Parallelism | Server Tool Parser | Agent Tool Mode | Outcome | Date |
|---|---|---|---|---|---:|---|---:|---|---|---|---|
| Carina | Maximum capability | 2 x L40S, 48 GB each | [Muse-Glimmer-30B](https://huggingface.co/meta-models/Muse-Glimmer-30B/tree/a4e59da52a7bc87ae7251dd5545c0dd437c44b68) | BF16 | 32,768 | CUDA graphs | 2 | `muse_glimmer` tool and reasoning | OpenHands native tools | Qualified | 2026-08-14 |
| Terra | Powerful | 2 x T4, 16 GB each | [Qwen3-Coder-30B-A3B-Instruct-W4A16-mixed-AWQ](https://huggingface.co/YCWTG/Qwen3-Coder-30B-A3B-Instruct-W4A16-mixed-AWQ/tree/e69e73813144d9b715648d8384b3f2c035397411) | W4A16 AWQ | 18,432 | Eager | 2 | `qwen3_coder` | OpenHands native tools | Qualified | 2026-08-15 |

All listed model repositories declare the Apache-2.0 license at the pinned revision.
Confirm that a model's license and intended use remain suitable for the project before downloading it.
The Carina qualification observed NVIDIA driver `590.48.01`; the Terra qualification observed driver `535.154.05`.

## Historical Qualifications

| Platform | Configuration | Qualified Date | Qualified Runtime | Current Status |
|---|---|---|---|---|
| Carina, 1 x L40S | Qwen3 Coder 30B FP8 | 2026-07-21 | vLLM `0.25.1+cu129` | Requalification required after the runtime update |

Historical results document combinations that passed an earlier release contract.
They are not current recommendations because changing the inference runtime expires the qualification.
The snapshot remains available under advanced model choices for explicit requalification.

## Unsupported Configurations

| Platform | Configuration | Tested Runtime | Date | Result |
|---|---|---|---|---|
| Terra, 1 x T4 | Qwen2.5 Coder 7B AWQ | vLLM `0.25.1+cu129` | 2026-07-21 | Unsupported for the tested tuple: direct inference worked, but the required OpenHands tool-use workflow did not pass. |
| Terra, 1 x T4 | Qwen2.5 Coder 14B AWQ | vLLM `0.25.1+cu129` | 2026-07-21 | Unsupported for the tested tuple: direct inference worked, but the required OpenHands tool-use workflow did not pass. |
| Terra, 4 x T4 | Qwen3 Coder 30B FP8 | vLLM `0.25.1+cu129` | 2026-07-21 | Unsupported for the tested tuple: the FP8 Mixture-of-Experts kernel cannot load this model's quantization dimensions on T4 hardware. |
| Terra, 4 x T4 | GPT-OSS 20B MXFP4 | vLLM `0.25.1+cu129` | 2026-07-21 | Unsupported on T4: vLLM requires compute capability 8.0 or newer, while T4 provides 7.5. |
| Terra, 4 x T4 | GPT-OSS 120B MXFP4 | vLLM `0.25.1+cu129` | 2026-07-21 | Unsupported on T4: the same MXFP4 runtime requires compute capability 8.0 or newer, while T4 provides 7.5. |
| Terra, 4 x T4 | Qwen3 Coder 30B W4A16 AWQ with tensor parallelism 4 | vLLM `0.25.1+cu129` | 2026-07-21 | Unsupported for the tested tuple: the quantization group size crosses four-way tensor shards. |

Unsupported configurations are retained only as compatibility evidence.
They are not model choices and cannot be recommended or downloaded from the managed catalog.

## Inconclusive Attempts

| Platform | Configuration | Tested Runtime | Date | Result |
|---|---|---|---|---|
| Carina, 2 x L40S | GPT-OSS 120B MXFP4 | vLLM `0.25.1+cu129` | 2026-07-22 | The download was interrupted and the allocation attempt stopped before model startup because the platform detector reported no compatible two-GPU capacity. |
| Carina, 2 x L40S | Qwen3 Coder Next FP8 | vLLM `0.25.1+cu129` | 2026-07-22 | vLLM reached distributed NCCL initialization but did not become ready or produce coding-agent qualification evidence. |

Inconclusive does not mean the model is incompatible.
It means the exact attempt did not produce enough evidence to qualify or reject the configuration.

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
Not-tested configurations are never added to the recommended set or selected automatically.
Historical, unsupported, and inconclusive results remain in this evidence record so Heartwood does not treat them as current recommendations or automatically retry failed combinations.

## Unsupported Hardware

The CUDA 12.9 runtime requires an NVIDIA GPU with compute capability 7.5 or newer.
Heartwood therefore stops before model startup on P4, P100, and V100 GPUs.
Choose a T4 or newer GPU, use a hosted model route, or select the portable CPU runtime instead.

Use `heartwood doctor` for the environment summary and `heartwood runtime start --dry-run` for the complete model and allocation plan.
Do not bypass a compatibility failure by changing vLLM, PyTorch, CUDA, the model revision, tensor parallelism, or parser inside a released environment; that creates a custom configuration without qualification evidence.
