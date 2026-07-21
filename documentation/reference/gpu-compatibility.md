<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# GPU Compatibility

Heartwood keeps the NVIDIA inference runtime and its model configurations in a release-owned compatibility matrix.
The matrix separates configurations that have completed the full coding-agent qualification from candidates that are still under evaluation.
Heartwood exposes candidates in advanced model selection, but it does not label or automatically select them as recommendations.

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

## Model Configurations

| Platform | Capability Tier | GPU | Model and Immutable Revision | Precision | Context | Execution | Tensor Parallelism | Server Tool Parser | Agent Tool Mode | Status |
|---|---|---|---|---|---:|---|---:|---|---|---|
| Terra | Standard | 1 x T4, 16 GB | [Qwen2.5-Coder-7B-Instruct-AWQ](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-AWQ/tree/8e8ed243bbe6f9a5aff549a0924562fc719b2b8a) | AWQ int4 | 18,432 | Eager | 1 | `hermes` | OpenHands prompt conversion | Qualified |
| Terra | Powerful | 1 x T4, 16 GB | [Qwen2.5-Coder-14B-Instruct-AWQ](https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct-AWQ/tree/eb3172f06a6d6b3a15f08947b0668d782e4d2d2c) | AWQ int4 | 18,432 | Eager | 1 | `hermes` | OpenHands prompt conversion | Qualified |
| Carina | Standard fallback | 1 x L40S, 48 GB | [Qwen2.5-Coder-7B-Instruct-AWQ](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-AWQ/tree/8e8ed243bbe6f9a5aff549a0924562fc719b2b8a) | AWQ int4 | 32,768 | CUDA graphs | 1 | `hermes` | OpenHands prompt conversion | Candidate |
| Carina | Powerful | 1 x L40S, 48 GB | [Qwen3-Coder-30B-A3B-Instruct-FP8](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8/tree/dcaee4d4dfc5ee71ad501f01f530e5652438fde0) | FP8 | 32,768 | CUDA graphs | 1 | `qwen3_coder` | OpenHands prompt conversion | Candidate |
| Carina | Powerful | 2 x L40S, 48 GB each | [Qwen3-Coder-30B-A3B-Instruct](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct/tree/b2cff646eb4bb1d68355c01b18ae02e7cf42d120) | BF16 | 65,536 | CUDA graphs | 2 | `qwen3_coder` | OpenHands prompt conversion | Candidate |
| Carina | Maximum capability | 4 x L40S, 48 GB each | [Qwen3-Coder-Next-FP8](https://huggingface.co/Qwen/Qwen3-Coder-Next-FP8/tree/da6e2ed27304dd39abadd9c82ef50e8de67bdd4c) | FP8 | 65,536 | CUDA graphs | 4 | `qwen3_coder` | OpenHands prompt conversion | Candidate |
| Carina | Maximum capability alternative | 2 x L40S, 48 GB each | [GPT-OSS 120B](https://huggingface.co/openai/gpt-oss-120b/tree/b5c939de8f754692c1647ca79fbf85e8c1e70f8a) | MXFP4 | 65,536 | CUDA graphs | 2 | `openai` | OpenHands prompt conversion | Candidate |

All listed model repositories declare the Apache-2.0 license at the pinned revision.
Confirm that a model's license and intended use remain suitable for the project before downloading it.

The Qwen3 Coder 30B FP8 snapshot was also tested with four T4 GPUs and vLLM 0.25.1 on CUDA 12.9.
That combination is unsupported because the FP8 Mixture-of-Experts kernel cannot load the model's quantization dimensions on T4 hardware.

## Qualification Requirement

A configuration becomes **qualified** only after the exact model revision and locked runtime complete one bounded Heartwood task on the named platform.
The acceptance test must establish all of the following:

1. the model loads and returns a direct inference response;
2. OpenHands converts the model response into a structured tool proposal;
3. Heartwood presents the complete action set for approval;
4. approval executes the proposed operation and modifies only the synthetic project;
5. an independent check verifies the exact file result;
6. a fresh process replays the session; and
7. audit export validates event coverage, hash-chain integrity, and content scrubbing.

The result records the GPU model, count, memory, driver, runtime versions, model revision, context size, tensor parallelism, server parser, and agent tool mode.
A candidate remains visible for evaluation but cannot become an automatic recommendation until that result passes.

## Unsupported Hardware

The CUDA 12.9 runtime requires an NVIDIA GPU with compute capability 7.5 or newer.
Heartwood therefore stops before model startup on P4, P100, and V100 GPUs.
Choose a T4 or newer GPU, use a hosted model route, or select the portable CPU runtime instead.

Use `heartwood doctor` for the environment summary and `heartwood runtime start --dry-run` for the complete model and allocation plan.
Do not bypass a compatibility failure by changing vLLM, PyTorch, CUDA, the model revision, tensor parallelism, or parser inside a released environment; that creates a new unqualified configuration.
