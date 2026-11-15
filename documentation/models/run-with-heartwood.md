<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Run a Model With Heartwood

Heartwood images and the native packages include inference software but no model weights.
Once a supported model is downloaded or imported, `heartwood` verifies its files, plans the context against available memory, starts the runtime, waits until it is ready, and opens the requested interface.
A cached model avoids the download, but Heartwood still verifies its source revision and file hashes before every start.
Heartwood-managed models run for local launches; an isolated worker uses the HTTPS model endpoint set in its deployment manifest.

## Download and Start

```bash
heartwood
```

Choose **Run with Heartwood**, inspect the plan, and confirm the download.
The terminal shows transferred bytes and status; the browser shows a progress bar from the same gateway download state.

In the terminal, Heartwood continues automatically after the verified download and starts the runtime.
In the browser setup flow, stop the process and run `heartwood --interface web` again when the page reports that the model is ready; the new process verifies the model, starts the runtime and supervises it for the session.

Stopping the gateway cancels unfinished downloads and waits for their workers.
A shutdown timeout means work is still running; wait for it and retry the shutdown before moving project state.
Verified model files remain available for the next launch.

## Runtime Connection and Lifetime

The launcher gives llama.cpp or vLLM a private Unix socket in an owner-only temporary directory outside the project.
Readiness checks, model discovery and inference use that socket, and no managed TCP listener is opened; the `http://127.0.0.1:8765/v1` address in model settings is a logical route for the SDK's private transport, not a port to connect to.
A missing socket or altered route stops inference instead of falling back to another listener.
If the runtime exits, the launcher stops the interaction, and it removes the socket directory on shutdown; a new runtime needs a new Heartwood process.
Other processes running as the same user keep that user's access; the isolated worker deployment is the boundary that separates agent code from the runtime.

## Hardware

| Runtime | Model Shape | Typical Environment |
|---|---|---|
| llama.cpp | One GGUF file | CPU workstation, standard container or native Linux |
| vLLM | Standard safetensors snapshot | NVIDIA GPU container, Terra GPU image or a Carina Slurm GPU allocation |

Heartwood reports model-specific minimum and recommended resources before download.
For GPU configurations it also checks the catalog GPU count, per-device memory, driver, precision, tool parser, tensor-parallel layout and qualification status against the [GPU compatibility matrix](../reference/gpu-compatibility.md).
No static estimate covers every architecture, driver, concurrent workload or platform limit.

## Context Window

Heartwood records the model's declared limit and chooses a conservative operating tier that fits the available memory.

| Tier | OpenHands Input | Reserved for the Response |
|---:|---:|---:|
| 18,432 | 16,384 | 2,048 |
| 32,768 | 28,672 | 4,096 |

18,432 tokens is the smallest agent-capable tier, and Heartwood uses it whenever model-size or memory information is unavailable.
Larger tiers range from 65,536 up to the model limit and Heartwood's maximum of 1,048,576 tokens.
Before history exceeds the input budget, OpenHands condenses it, keeping recent events and a summary.
A larger window helps with broad repositories and long analyses but increases key/value-cache memory and latency, so prefer the largest window that fits with headroom over the value on a model card.

## Advanced Runtime Control

Start with `heartwood`.
Operators can inspect or control runtime allocation separately:

```bash
heartwood runtime start --dry-run
heartwood runtime start --task-profile powerful --partition dev --time 01:00:00
```

On Carina, Heartwood prints the complete Slurm request and asks before allocating GPUs; on Terra it uses the attached resources without a scheduler request.
Download and allocation are confirmed separately.
`--dry-run` can show a recommendation when no model is selected, but never saves it; complete setup before starting or allocating the runtime.

## Stop the Runtime

Exit Heartwood to stop the supervised runtime.
Downloaded model files remain in `.heartwood/models/` and are verified again on the next start.
