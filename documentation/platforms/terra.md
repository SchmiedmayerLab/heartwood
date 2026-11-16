<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood on Terra

[Terra](https://terra.bio/) provides cloud workspaces, Jupyter applications, compute, persistent storage and access controls for biomedical research.
The Heartwood Terra images extend Terra's Jupyter environment with the `heartwood` terminal, the notebook bridge, verified Skills and optional managed inference.
Terra supports the Heartwood terminal and notebook interfaces; it has no supported route to the browser interface.

This guide creates cloud compute that Terra charges for.
Review the price Terra shows before creating the environment, and use synthetic or non-sensitive data.

## Qualification

The Terra images are built from the Terra Jupyter base and tested in CI for Jupyter inheritance, the prefixed internal gateway route, persistence and the Docker manifest shape Terra requires.
The Terra adapter reports validation level `ci`; CI does not start a live Terra workspace.
The two-T4 Qwen3 Coder 30B AWQ configuration completed Heartwood's coding-agent qualification in a Terra workspace on 2026-08-15 with the pinned vLLM runtime listed in [GPU Compatibility](../reference/gpu-compatibility.md).
A Terra launch is a local launch with `.heartwood/` state on the persistent disk; the isolated worker deployment is not qualified on Terra.
Operators preparing Terra for a group should also [record a standard environment](../operate/index.md#record-a-standard-environment).

## Before You Begin

You need a Terra workspace and permission to create or replace its Jupyter Cloud Environment.
Terra accepts custom images derived from a Terra base image; see Terra's [custom Jupyter environment guide](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks).

## Choose the Image and Compute

Terra does not apply a new image tag or GPU configuration to an existing Cloud Environment.
To change either, delete the Cloud Environment and create a new one; deleting the environment and deleting its persistent disk are separate choices, so keep the disk if it holds files you need.
See Terra's [GPU Cloud Environment guide](https://support.terra.bio/hc/en-us/articles/4403006001947-Getting-started-with-GPUs-in-a-Cloud-Environment).

In the workspace's Jupyter Cloud Environment settings:

1. Select **Customize**, then **Custom Environment** under application configuration.
2. Select the CPU and memory combination from the table below.
3. Enable the GPU when required and check its type and count.
4. Set the persistent-disk size and auto-pause interval.
5. Enter the container image last and wait until auto-detection enables **Next**.
6. Review every value, then select **Create**.

Changing the CPU choice can reset the image or GPU selection, which is why the image goes last.

| Model Route | Image | Starting Point |
|---|---|---|
| Stanford AI API Gateway or a hosted service | `ghcr.io/schmiedmayerlab/heartwood:0.4.0-terra` | 8 CPUs, 30 GB RAM, 50 GB persistent disk |
| Run with Heartwood: Qwen3 Coder 30B AWQ | `ghcr.io/schmiedmayerlab/heartwood:0.4.0-terra-gpu-nvidia` | 32 CPUs, 120 GB RAM, two T4 GPUs with 16 GB each, 200 GB persistent disk |

A hosted model is the shortest first run.
For a Heartwood-managed CPU model, whose catalog recommendation is 32 GB RAM, use 16 CPUs and 60 GB RAM instead.
The GPU qualification applies to two T4 GPUs at an 18,432-token context; one- and four-T4 results are separate records and do not carry over.
Heartwood stops before starting vLLM on P4, P100 or V100 GPUs, whose compute capability is below the supported floor.

For the first model download, verification and startup, set auto-pause to at least 120 minutes; the pinned model is about 17 GiB, and the first run can stay active well beyond 15 minutes while Heartwood reports each stage.
Shorten auto-pause afterwards.
The persistent disk needs the model's recommended free space in addition to existing notebooks, project files and `.heartwood/`; check it with `df -h /home/jupyter` before downloading.
For a project that already contains models or other analyses, start from 250 GB rather than 200 GB.
See [Choose a Heartwood-Managed Model](../models/choose-managed.md) for sizes and Terra's [Jupyter app guide](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app) for disk changes.

## Create and Verify the Environment

Select **Create**, wait for Jupyter to become ready, open it, and select **File -> New -> Terminal**.

```bash
heartwood --version
```

The version must match the image tag.
If it reports an older release, Terra is still running the previous image: delete and recreate only the Cloud Environment with the intended tag, keep the persistent disk, and check again.
Do not install another copy of Heartwood into the environment.

## Create a Project Directory

Terra keeps files below `/home/jupyter` while the persistent disk exists.
Create a dedicated child directory so that the project does not include unrelated notebooks or files:

```bash
mkdir -p /home/jupyter/heartwood-project
cd /home/jupyter/heartwood-project
heartwood doctor
```

This directory is the project for every Heartwood command and notebook started there, and its state is in `/home/jupyter/heartwood-project/.heartwood/`.
Workspace sharing and Authorization Domains decide who can read workspace data; see Terra's [Cloud Environment FAQ](https://support.terra.bio/hc/en-us/articles/360057425291-Cloud-Environment-FAQs) for persistent-disk behavior.

## Choose a Model and Start the Terminal

```bash
heartwood
```

Heartwood confirms the project and asks where the model runs:

- **Stanford AI API Gateway**, when the route is approved for the Terra project;
- **Sign in with ChatGPT**, **OpenAI API**, **Anthropic** or **Other compatible service**, only when that service is authorized for the intended data; or
- **Run with Heartwood**, to download and serve model weights inside the Terra environment.

On two T4 GPUs, Heartwood recommends **Qwen3 Coder 30B AWQ** and shows its resources, license and download size before asking.
Other catalog entries and **Other Hugging Face model** are available for explicit evaluation; Heartwood reports an unsupported-model error when the runtime cannot serve a choice.

Download progress appears in the terminal, and files persist under `.heartwood/models/`.
Before vLLM starts, Heartwood verifies every model file; allow 10-15 minutes for verification on a persistent disk and another 2-15 minutes for startup.
Importing an existing model directory copies and verifies it, needs another model-sized block of free space and can take considerably longer.

Use `heartwood --plain` if the full-screen terminal does not render correctly, and see [Use the Terminal](../use/terminal.md) for the conversation and review workflow.

## Use the Notebook Bridge

Open a notebook inside the project directory and select the **Python 3 (Heartwood)** kernel.
The default Terra Python kernel does not contain the Heartwood packages.

```python
from pathlib import Path
from heartwood.notebook import NotebookSession

project_root = Path.cwd().resolve()
session = NotebookSession(session_id="terra-notebook-analysis")
session.startup_plan()
```

Use a distinct session identifier while the terminal is open.
With a Heartwood-managed model, keep the terminal process that supervises the model running while the notebook submits requests.
Continue with [Use Heartwood From a Notebook](../use/notebooks.md); the [Terra notebook](../assets/examples/terra-heartwood.ipynb) is an output-free synthetic starting point.

## Try a Bounded Synthetic Task

```text
Inspect the synthetic analysis files in this project. Summarize the cohort inclusion and exclusion rules, run only the existing synthetic validation command, and report the aggregate participant count. Do not access parent directories or network resources, and do not export row-level data.
```

Review every proposed operation before allowing the action set, and check the resulting files and output yourself; an agent response is not scientific validation.

## Preserve Results and Stop Compute

Exit Heartwood with `/exit`, copy durable results to workspace storage, and pause or delete the Cloud Environment when finished.
Retained resources continue to incur charges.
Deleting the persistent disk deletes `.heartwood/` and any project files stored only there.

## Troubleshooting Terra

- If Jupyter returns 404, open Jupyter from the Terra workspace rather than a guessed URL.
- If creation fails with `ZONE_RESOURCE_POOL_EXHAUSTED`, the requested resources are unavailable in Terra's default zone and the container did not start. Delete only the failed Cloud Environment, keep the disk, and retry later; Terra's [Swagger API procedure](https://support.terra.bio/hc/en-us/articles/4403307463067-How-to-create-a-custom-Cloud-Environment-with-the-Swagger-API) can select another zone.
- If a Heartwood browser URL returns 401 or 404, use the terminal or notebook; the browser interface is not supported on Terra.
- If `import heartwood` fails in a notebook, switch to the **Python 3 (Heartwood)** kernel and restart it.
- If a model download stops, rerun Heartwood from the same project; completed files are reused, and the model is published into `.heartwood/models/` only after full verification.
- If Heartwood reports insufficient storage, run `df -h /home/jupyter`, keep needed files, and recreate only the Cloud Environment with a larger disk.
- If model startup is slow or fails, compare the printed model plan with the attached RAM, GPU memory and disk, then inspect `.heartwood/logs/local-model.log`.
- If Heartwood reports an unsupported P4, P100 or V100, recreate the Cloud Environment with T4 GPUs and keep the disk; do not replace the vLLM or PyTorch packages in place.
- If the GPU is not detected, confirm that the GPU image and a GPU were selected together, then run `nvidia-smi` and `heartwood doctor`.
- If Terra rejects the image during auto-detection, confirm that the tag ends in `-terra` or `-terra-gpu-nvidia`; only these tags use the single-platform manifest Terra accepts.
- If `heartwood --version` does not match the tag, recreate the Cloud Environment and keep the disk; resuming an environment does not update its image.

`heartwood doctor` reports `HW-TERRA-*` diagnostics with recovery steps.
