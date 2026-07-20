<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Run a Model With Heartwood

Heartwood images include managed inference software but no model weights.
After a supported model is selected and downloaded or imported, the normal `heartwood` command verifies the files, plans context against available memory, starts the runtime, waits for readiness, and then opens the requested interface.

## Download and Start

The guided path is:

```bash
heartwood
```

Choose **Run with Heartwood**, inspect the plan, and confirm the download.
The terminal displays transferred bytes and status; the browser shows a progress bar from the same gateway download state.

When the files are ready, run `heartwood` again if the current process asks you to restart.
For the browser, use `heartwood --interface web`.

## Hardware

| Runtime | Model Shape | Typical Environment |
|---|---|---|
| llama.cpp | One GGUF file | CPU workstation or portable container |
| vLLM | Standard safetensors snapshot | NVIDIA-enabled container, Terra GPU runtime, or Carina Slurm GPU allocation |

Heartwood reports model-specific minimum and recommended guidance before download.
The estimates reserve space for runtime overhead and context, but no static estimate can account for every model architecture, driver, concurrent workload, or platform limit.

## Context Window

Heartwood records the model's declared limit and chooses a power-of-two operating tier from 16,384 tokens upward when memory information permits.
It uses a 32,768-token safe default when model-size or memory information is unavailable, and can plan larger windows up to the model and Heartwood maximum of 1,048,576 tokens.

The planner reserves memory for model weights, runtime overhead, and context, and retains headroom instead of always selecting the model maximum.
The OpenHands SDK backend uses a rolling-history condenser before history exceeds the configured input budget, preserving recent events and a structured summary for long sessions.

Larger context is useful for broad repositories and long analyses but increases key/value-cache memory and latency.
Prefer the largest window that fits with conservative headroom rather than the largest value printed on a model card.

## Advanced Runtime Control

Normal users should start with `heartwood`.
Operators can inspect or control runtime allocation separately:

```bash
heartwood runtime start --dry-run
heartwood runtime start --partition dev --time 01:00:00
```

On Carina, Heartwood prints the complete Slurm request and asks before allocating a GPU.
On provisioned Terra compute, it uses the attached resources without submitting a scheduler request.

## Stop the Runtime

Exit the Heartwood process to stop the supervised model runtime.
Downloaded model files remain in `.heartwood/models/` and are verified again on the next start.
