<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood on Terra

[Terra](https://terra.bio/) provides cloud workspaces, Jupyter applications, compute, persistent storage, and access controls for biomedical research.
The Heartwood Terra image extends Terra's Jupyter environment with the terminal agent, notebook bridge, verified Skills, and optional managed inference.

Terra supports the Heartwood **terminal** and **notebook** interfaces.
It does not currently expose a supported route to the Heartwood browser interface.

This guide changes cloud compute and can incur charges.
Begin with synthetic or non-sensitive data and review the price shown by Terra before creating the environment.

## Before You Begin

You need access to a Terra workspace and permission to create or replace its Jupyter Cloud Environment.
Terra supports custom images derived from a Terra base image; see Terra's [custom Jupyter environment guide](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks).

## Choose the Image and Compute

If the workspace already has a Jupyter Cloud Environment and you want to change its Heartwood image tag or GPU configuration, delete that Cloud Environment before continuing.
Terra does not apply those changes to an existing environment; create a new environment with the required Heartwood image and compute.
Deleting the compute environment and deleting its persistent disk are separate choices, so retain the disk when it contains project files you still need.
See Terra's [GPU Cloud Environment guide](https://support.terra.bio/hc/en-us/articles/4403006001947-Getting-started-with-GPUs-in-a-Cloud-Environment).

Open the workspace's Jupyter Cloud Environment settings and configure the environment in this order:

1. Select **Customize**, then choose **Custom Environment** under application configuration.
2. Select the CPU and memory combination from the table below.
3. Enter the corresponding container image.
4. Enable the GPU, when required, and verify the GPU type and count.
5. Set auto-pause and review every value before selecting **Create**.

Terra can reset the image or GPU selection when the CPU choice changes, so set compute resources first and verify the complete form before creation.

Use one of these combinations:

| Model Route | Image | Practical Starting Point |
|---|---|---|
| Research environment or hosted service | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.7-terra` | 8 CPUs, 30 GB RAM, 50 GB persistent disk |
| Heartwood-managed CPU inference | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.7-terra` | 16 CPUs, 60 GB RAM, 75 GB persistent disk |
| Qualified managed GPU inference | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.7-terra-gpu-nvidia` | 32 CPUs, 120 GB RAM, two T4 GPUs with 16 GB each, 200 GB persistent disk |

A hosted model is the shortest first run.
Use the GPU image for a capable model managed inside the Terra environment.
CPU inference is portable but can be too slow for an interactive coding workflow.

These are starting points rather than universal requirements.
Terra's current standard machine choices pair 8 CPUs with 30 GB RAM and 16 CPUs with 60 GB RAM.
The 16 CPU option preserves the catalog's recommended system-memory headroom; 8 CPUs and 30 GB RAM is a lower-cost evaluation configuration that may leave less room for model loading and concurrent notebook work.
The GPU path exposes one release-pinned Terra recommendation.
The qualified two-T4 recommendation is Qwen3 Coder 30B W4A16 AWQ with a conservative 18,432-token context.
Heartwood reports the detected GPU, memory, driver, model cache, and compatible catalog entries before startup.
It stops before launching modern vLLM on P4, P100, or V100 GPUs because their compute capability is below the supported floor.
For the first model download and startup, set auto-pause to at least 120 minutes; image creation, model verification, and inference startup can each take several minutes without terminal output from the model itself.
After setup is complete, shorten auto-pause to match the normal research workflow.

Heartwood inspects model size and available memory before launch, chooses a context capacity with response headroom, and warns when the selected compute is below its conservative estimate.
On a 16 GB T4, Heartwood uses eager vLLM execution to avoid the additional GPU-memory peak from CUDA graph capture.
Larger GPU memory can enable context capacities above 32K when the model supports them, but increasing context also increases GPU-memory use and response latency.
See [Choose a Heartwood-Managed Model](../models/choose-managed.md) for download and resource estimates and [GPU Compatibility](../reference/gpu-compatibility.md) for exact runtime combinations.

Retain the persistent disk when replacing compute and copy valuable results to workspace storage.
See [Starting and Customizing Your Jupyter App](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app).

## Create and Verify the Environment

Select **Create** and wait for Jupyter to become ready.
Open Jupyter, then select **File -> New -> Terminal**.

Verify the installed release:

```bash
heartwood --version
```

The reported version must match the image tag in this guide.
If it reports an older release, Terra is still running the previous immutable image: delete and recreate only the Cloud Environment with the intended tag, retain the persistent disk, and verify the version again before setup.

Do not install another Heartwood copy into the environment.
The custom image already contains the tested command, notebook kernel, Skills, and inference runtime while preserving Terra's base-image behavior.

## Create a Project Directory

Terra preserves files below `/home/jupyter` while the persistent disk is retained.
Create a dedicated child directory so the agent boundary does not include unrelated notebooks or files:

```bash
mkdir -p /home/jupyter/heartwood-project
cd /home/jupyter/heartwood-project
heartwood doctor
```

This directory is the project for every Heartwood command and notebook started there.
Configuration, model files, sessions, and audit records remain under `/home/jupyter/heartwood-project/.heartwood/`.
Terra documents persistent-disk behavior and deletion precautions in its [Cloud Environment FAQ](https://support.terra.bio/hc/en-us/articles/360057425291-Cloud-Environment-FAQs).

## Choose a Model and Start the Terminal

Run:

```bash
heartwood
```

The first-use flow confirms the project and asks where the model runs.

- Choose a research-environment connection when the Terra deployment supplies an approved service.
- Choose OpenAI, Anthropic, or **Other compatible service** only when that endpoint is authorized for the intended data.
- Choose **Run with Heartwood** to download and serve model weights inside the Terra environment.

For qualified managed coding-agent inference, start with the **Powerful** Qwen3 Coder 30B W4A16 AWQ recommendation on two T4 GPUs.
You can instead choose **Other Hugging Face model** and enter another public repository.
Heartwood inspects its metadata and reports a clear unsupported-model error when the available runtime cannot serve it safely.

The pinned Qwen3 Coder 30B W4A16 AWQ snapshot downloads about 16.8 GiB; use at least 96 GB RAM, retain a 200 GB persistent disk, and keep the catalog's 18,432-token context so the two T4 GPUs retain key/value-cache headroom.
Model download progress appears in the terminal and files persist under `.heartwood/models/`.
Running `heartwood models download MODEL` is itself an explicit request to download that model; the guided `heartwood` flow presents the selected model and asks before downloading it.
Depending on the model and persistent-disk throughput, the first verification and inference startup is planned for approximately 2-15 minutes while Heartwood verifies the snapshot and vLLM prepares GPU memory.
Heartwood reports the active stage, elapsed time, selected context capacity, and memory assessment while you wait.

Use `heartwood --plain` when the full-screen terminal is not rendered correctly.
See [Use the Terminal](../use/terminal.md) for conversations, grouped action review, replay, and audit export.

## Use the Notebook Bridge

Create or open a notebook inside the same project directory and select the **Python 3 (Heartwood)** kernel.
The default Terra Python kernel and the `python` command in a terminal may not contain the Heartwood packages; selecting the named kernel is required.

Verify the boundary in the first cell:

```python
from pathlib import Path
from heartwood.notebook import NotebookSession

project_root = Path.cwd().resolve()
session = NotebookSession(session_id="terra-notebook-analysis")
session.startup_plan()
```

Use a distinct session identifier if the terminal is open at the same time.
When a Heartwood-managed model is selected, keep the terminal process that supervises the model running while the notebook submits requests.

Follow [Use Heartwood From a Notebook](../use/notebooks.md) to inspect readiness, submit a task, review a grouped action set, and export the audit record.
The [downloadable Terra notebook](../assets/examples/terra-heartwood.ipynb) is an output-free synthetic starting point.

## Try a Bounded Synthetic Task

Start with a request that names the allowed files, expected result, and verification:

```text
Inspect the synthetic analysis files in this project. Summarize the cohort inclusion and exclusion rules, run only the existing synthetic validation command, and report the aggregate participant count. Do not access parent directories or network resources, and do not export row-level data.
```

Review every proposed operation before allowing the grouped action set.
Inspect the resulting files and command output independently; an agent response is not scientific validation.

## Preserve Results and Stop Compute

Exit Heartwood with `/exit`.
Save project files on the persistent disk and copy durable results to workspace storage according to the research workflow.
Pause or delete the Cloud Environment when finished and decide whether the persistent disk should remain.

Terra charges can continue for retained resources.
Deleting the persistent disk removes `.heartwood/` and project files stored only there.

## Troubleshooting Terra

- If Jupyter returns **404**, open Jupyter from the Terra workspace rather than using a guessed host path.
- If creation fails with `ZONE_RESOURCE_POOL_EXHAUSTED`, the requested Google Cloud resources are temporarily unavailable in Terra's default zone; the container has not started. Delete only the failed Cloud Environment, retain its persistent disk, and retry later. Terra also documents an [advanced Swagger API procedure](https://support.terra.bio/hc/en-us/articles/4403307463067-How-to-create-a-custom-Cloud-Environment-with-the-Swagger-API) for selecting another zone; use it only when you are comfortable creating and tracking a Cloud Environment outside the standard form.
- If a Heartwood browser URL returns **401** or **404**, use the terminal or notebook interface; browser access is not supported on Terra.
- If `import heartwood` fails in a notebook, switch the notebook kernel to **Python 3 (Heartwood)** and restart the kernel.
- If a model download stops, rerun Heartwood from the same project; verified files in `.heartwood/models/` are reused.
- If model startup is slow or fails, compare the printed model plan with attached RAM, GPU memory, and persistent-disk space, then inspect `.heartwood/logs/local-model.log` from the same project.
- If Heartwood reports an unsupported P4, P100, or V100, delete and recreate the Cloud Environment with a T4 while retaining the persistent disk; do not replace the released vLLM or PyTorch packages in place.
- If the GPU is not detected, confirm that the GPU image and GPU were selected together, then run `nvidia-smi` and `heartwood doctor` from the project terminal.
- If Terra rejects the image during auto-detection, confirm that the tag ends in `-terra` or `-terra-gpu-nvidia`; these tags use the single-platform manifest format required by Terra's Leonardo service.
- If `heartwood --version` does not match the requested image tag, replace the Cloud Environment while retaining the persistent disk; resuming an existing environment does not update its image.
- Run `heartwood doctor` for stable `HW-TERRA-*` recovery guidance.
