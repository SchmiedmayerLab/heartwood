<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Use Heartwood on Terra

Use the Terra image when the analysis already lives in a Terra workspace. It preserves Jupyter and adds the Heartwood terminal, browser application, notebook kernel, and optional local-model runtimes.

Begin in a synthetic workspace with no protected health information. A working image does not authorize a dataset or model route.

## Before You Begin

You need permission to create or replace a Terra Jupyter Cloud Environment, a retained persistent disk, and one authorized model route. A local model also requires enough disk, RAM, and GPU memory for the selected artifact.

| Goal | Image | Starting resources |
|---|---|---|
| Hosted or Stanford gateway model | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra` | 8 CPUs, 30 GB RAM, 50 GB persistent disk |
| Portable CPU local model | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra` | 8 CPUs, 32 GB RAM, at least 75 GB persistent disk |
| Interactive NVIDIA local model | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra-gpu-nvidia` | 8 CPUs, 48 GB RAM, one T4-class GPU or better, at least 75 GB persistent disk |

These are tutorial starting points, not universal requirements. A hosted model is the shortest path. Use the NVIDIA image for interactive local inference; the CPU path is considerably slower.

## 1. Create the Cloud Environment

In the Terra workspace:

1. Open **Analyses**.
2. Open the Jupyter Cloud Environment settings.
3. Choose a custom image under **Application configuration**.
4. For local GPU inference, enable a GPU before finalizing the other compute fields.
5. Paste exactly one versioned Heartwood Terra image from the table above.
6. Choose the machine, memory, persistent disk, and autopause settings.
7. Review Terra's cost estimate and create or replace the environment.

Keep the persistent disk when replacing the compute environment. Terra stores Jupyter persistent-disk files under `/home/jupyter`. Provider tokens held by a Heartwood process do not persist.

When startup completes, confirm that normal Jupyter opens and that **Python 3 (Heartwood)** appears in the kernel list. Do not install another copy of Heartwood inside the image.

## 2. Create a Project

Open the Terra terminal:

```bash
mkdir -p /home/jupyter/heartwood-demo/input
cp /opt/heartwood/docs/terra-jupyter-demo.ipynb \
  /home/jupyter/heartwood-demo/
cp /opt/heartwood/fixtures/synthetic/omop-like/*.csv \
  /home/jupyter/heartwood-demo/input/
cd /home/jupyter/heartwood-demo

heartwood detect
heartwood doctor
```

`heartwood-demo` is only the tutorial directory. Any dedicated directory below `/home/jupyter` can be the project. Heartwood rejects `/home/jupyter` itself as unnecessarily broad and rejects paths outside the persistent mount.

The included CSV files are synthetic. Copy only the files that the agent should use into a real project directory.

## 3. Choose an Interface and Connect a Model

Choose the interface that will own the conversation. A hosted-provider token remains only in the process where it is entered; terminal, browser, and notebook processes do not share token values.

### Terminal

The terminal is the baseline and the fallback when a browser proxy is unavailable. Run:

```bash
heartwood
```

Guided setup offers **On this device**, **OpenAI**, **Anthropic**, and **Stanford AI API Gateway**. Enter any hosted-provider token at the private prompt, choose a discovered model, and continue the conversation in that terminal process.

For a downloaded local model, inspect the resource plan with `heartwood models local`, download a compatible choice, then use `heartwood launch`. Keep the terminal open while it supervises the model.

### Browser

Start the service:

```bash
heartwood serve
```

Open the authenticated route printed by the command and complete model setup in the browser. A token entered in the browser remains available to that service process.

For a downloaded local model, stop the setup service and run:

```bash
heartwood launch --web
```

Both commands print the authenticated Terra route when the required runtime metadata is available. Otherwise open `terra-jupyter-demo.ipynb` with the **Python 3 (Heartwood)** kernel and run its first code cell to generate **Open Heartwood in a new tab**.

Do not use `http://127.0.0.1:8767/` from your computer and do not guess a shortened `/proxy/8767/` path. Terra requires its complete authenticated Jupyter proxy URL.

### Notebook

The tutorial notebook can inspect readiness, display the browser link, submit a synthetic task, decide the complete action group, verify aggregate output, replay the session, and export the audit record. Save the non-secret model selection once through the terminal or browser before opening the notebook.

For a hosted model, supply the corresponding credential to the notebook kernel before creating `NotebookSession`. Enter it with `getpass` so it is not stored in the notebook:

```python
import os
from getpass import getpass

os.environ["STANFORD_AI_API_KEY"] = getpass("Stanford AI API Gateway token: ")
```

Use `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` instead when that is the selected profile. The notebook does not reuse a token held by a terminal or browser process.

The notebook does not start a downloaded model. Keep `heartwood launch --web` running in the terminal while notebook cells use a Heartwood-managed local model.

Terra's baseline policy does not accept an arbitrary Custom API URL. Platform operators must configure and authorize another institutional connection explicitly.

## 4. Try the Synthetic Tutorial

Choose one interface to own the turn and its action decision. Do not submit or approve the same active turn from another process.

The tutorial notebook uses the packaged synthetic OMOP-like fixture and repository-verified Skills. Its results demonstrate interface and tool integration only; they are not evidence of biomedical validity for another dataset.

After the turn becomes idle, replay the same session from the project terminal:

```bash
heartwood --session-id terra-demo replay
heartwood --session-id terra-demo audit export
```

## 5. Stop Safely

1. Exit the Heartwood conversation or press `Ctrl+C` in the process supervising the browser or local model.
2. Confirm that important project files are under `/home/jupyter/heartwood-demo`.
3. Copy durable deliverables to approved workspace storage when collaborators or workflows need them.
4. Pause or delete compute from Terra's Cloud Environment controls.
5. Keep the persistent disk when the project and downloaded models must survive.

Opening the terminal route may resume paused compute, so use Terra's dashboard to inspect the environment state.

## Terra Help

Use [Troubleshooting](troubleshooting.md) for Heartwood readiness, model, and proxy problems. Terra maintains current instructions for [custom Jupyter environments](https://support.terra.bio/hc/en-us/articles/5713716832027), [GPU environments](https://support.terra.bio/hc/en-us/articles/4403006001947), and [persistent disks](https://support.terra.bio/hc/en-us/articles/360047318551).
