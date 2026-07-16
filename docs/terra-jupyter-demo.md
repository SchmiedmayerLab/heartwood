<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Use Heartwood on Terra

Use the Terra image when an analysis already lives in a Terra workspace. It preserves Terra's Jupyter experience and adds the Heartwood terminal, browser interface, notebook kernel, and local-model support.

Start in a synthetic workspace containing no protected health information. The workflow below checks that the image, project storage, model connection, interfaces, action review, and audit record work together. It does not authorize a model, dataset, or use of controlled data.

## Choose Your Starting Path

Four choices determine the setup:

| Choice | Recommended starting point |
|---|---|
| Heartwood image | Use the portable Terra image for a hosted model or CPU-only local test. Use the NVIDIA Terra image for an interactive local model on a GPU. |
| Project | Create one analysis directory under `/home/jupyter`; that directory is the project Heartwood may modify. |
| Model | Prefer an institution-provided model when one is available. Otherwise use an authorized hosted provider or explicitly download a local model. |
| Interface | Start with the terminal. Add the browser through Terra's authenticated Jupyter proxy or use the example notebook after the project is configured. |

The terminal, browser, and notebook use the same model selection, sessions, action decisions, and `.heartwood/` project state. You do not configure three separate Heartwood installations.

## 1. Create the Cloud Environment

In the Terra workspace, open **Analyses**, select the cloud icon, open the Jupyter environment settings, and choose **Customize** or **Custom Environment** under **Application Configuration**. Paste one immutable release image:

```text
ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.2-terra
ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.2-terra-gpu-nvidia
```

The image name ending in `-terra` is the portable choice. The image ending in `-terra-gpu-nvidia` is required for Heartwood-managed NVIDIA inference. Both images contain the application and inference software but contain no model weights and no provider credentials.

These are practical starting configurations for the tutorial, not universal requirements:

| Setting | Portable image | NVIDIA image |
|---|---|---|
| CPUs | 16 | 8 or more |
| Memory | 60 GB | 48 GB or more |
| GPU | None | One NVIDIA T4 with 16 GB VRAM or better |
| Persistent disk | 100 GB | 100 GB |
| Autopause | 30 minutes | 30 minutes |
| Expected first-start wait | Allow up to 30 minutes | Allow up to 30 minutes |

Set the machine and persistent disk before selecting **Create** or **Create/Replace**, and review Terra's cost estimate. Keep the persistent disk when replacing or pausing the environment. Terra preserves files under `/home/jupyter` on that disk; important long-term results should also be copied to workspace storage according to the project's data-management policy.

| Item | Persists when the disk is retained? | Guidance |
|---|---|---|
| Project files and `.heartwood/` | Yes | Keep the complete project under `/home/jupyter`; deleting the disk deletes this state. |
| Downloaded local models | Yes | Models remain in the project's `.heartwood/models/` directory and consume persistent-disk space. |
| Conversations and audit records | Yes | They remain project-local and are available after pause, resume, or image replacement. |
| Provider tokens entered in Heartwood | No | Enter the token again after the Heartwood process restarts unless the platform supplies an approved secret binding. |
| Shared or archival results | Not automatically | The disk belongs to the individual cloud environment; copy required deliverables to approved workspace storage. |

When the environment reports that it is running, confirm that the normal Jupyter file browser opens and that **Python 3 (Heartwood)** appears in the kernel list. Terra may report a running environment before its **Open** control is ready. If **Open** remains unavailable, open the workspace terminal and follow its **Jupyter Notebook** link. Do not install Heartwood again inside the image.

## 2. Create a Project

Open a Terra terminal and create the synthetic tutorial project on the persistent disk:

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

The directory where the Heartwood command, web server, or notebook process starts is the project. `heartwood-demo` is only the tutorial name; there is no fixed Terra workspace path. Heartwood creates `.heartwood/` inside the project for configuration, sessions, models, Skills, logs, and audit data.

Heartwood file operations stay within the project and exclude its private `.heartwood/` directory. OpenHands terminal commands retain the operating-system permissions of the Jupyter process, so Terra's environment remains the hard filesystem boundary. Keep the whole project under `/home/jupyter` so both analysis files and Heartwood state survive pause, resume, and image replacement.

The tutorial fixture contains 24 synthetic people, 39 condition-occurrence rows, and 20 people with condition concept `201826`. Terra workspace tables and buckets are separate storage; localize only the files the agent should use into the project directory.

## 3. Connect a Model

Choose one model path. The first two avoid downloading model weights into Terra; the last two run inference in the Cloud Environment.

| Model path | What you need | Start with |
|---|---|---|
| Research environment | A model connection preconfigured by the platform or institution | Select **Research environment** in the setup flow. |
| Hosted provider | An authorized OpenAI, Anthropic, or OpenAI-compatible endpoint and its required credential | Select the provider in the setup flow and choose a model returned by its API. |
| Portable local model | The portable image, sufficient disk and RAM, and tolerance for slower CPU inference | Run `heartwood models local` and choose a CPU recommendation. |
| NVIDIA local model | The NVIDIA image, an attached compatible GPU, and sufficient disk, RAM, and GPU memory | Run `heartwood models local` and choose a GPU recommendation. |

For the simplest setup, run `heartwood` in the project terminal and follow the prompts. If you prefer visual setup, run `heartwood serve`, execute the tutorial notebook's first code cell to generate Terra's authenticated link, and complete the same choices in the browser. Provider credentials entered interactively are held by that Heartwood process and are not written into project configuration. The deploying institution must authorize the exact endpoint, identity, retention settings, and data classification; when required for controlled data, the route must be covered by an institution-approved business associate agreement.

For a connection whose credentials are already available to the current process or supplied by the platform, the CLI can refresh and select its model catalog:

```bash
heartwood models list
heartwood models refresh <connection-id>
heartwood models connect <connection-id> <model-id>
```

For a local model, inspect the available recommendations before downloading anything:

```bash
heartwood models local
```

Heartwood shows download size, runtime, context window, and conservative memory guidance. Continue with [Run a Local Model on Terra](#run-a-local-model-on-terra) after choosing one.

## 4. Choose an Interface

### Terminal

The terminal is the baseline interface and the best fallback when a browser route is unavailable. From the project directory, run:

```bash
heartwood
```

The first run opens setup when needed and otherwise enters the interactive conversation. For a downloaded local model, use `heartwood launch`; it starts the model server, waits for readiness, and then opens the same interactive terminal. Keep the terminal open while using the model.

### Browser

For a hosted or already-running model, start the browser service from the project directory:

```bash
heartwood serve
```

For a downloaded local model, start the model and browser together:

```bash
heartwood launch --web
```

Keep that terminal open. Then open the copied tutorial notebook with the **Python 3 (Heartwood)** kernel and run its first code cell. The cell displays **Open Heartwood in a new tab** using the current Terra runtime's authenticated route.

Do not use `http://127.0.0.1:8767/` or a generic `/proxy/8767/` URL from your computer. Terra requires the full `/proxy/<Google project>/<cluster>/jupyter/proxy/8767/` path, and Heartwood preserves that prefix for browser API requests. The CLI remains available if the platform proxy is temporarily unavailable.

![Heartwood synthetic reference analysis at a narrow notebook viewport](assets/web-notebook-viewport.png)

The screenshot shows the responsive layout used by the automated notebook-viewport test. It is not evidence of live Leonardo behavior or model quality.

### Notebook

[Open the Terra tutorial notebook](terra-jupyter-demo.ipynb) from the project directory with the Heartwood kernel. It can:

- identify the current project and display the authenticated browser link;
- inspect synthetic dataset proposals;
- submit a task through the same OpenHands-backed session contract;
- display the complete pending action set and apply one grouped decision;
- verify the generated aggregate result;
- replay the session and export the scrubbed audit record.

Initial model setup and Heartwood-managed runtime startup remain clearer in the terminal or browser. A local model must remain supervised by `heartwood launch --web` in a terminal while notebook cells use it.

## 5. Run the Synthetic Workflow

Choose one interface to own the task and its decision:

- **Notebook-owned turn:** run the tutorial notebook from top to bottom. Its detection cell creates session `terra-demo`; its task and approval cells submit and decide the turn. Do not submit or approve that turn in another interface.
- **Terminal-owned turn:** run `heartwood --session-id terra-demo`, submit the task, and decide the complete action group in that terminal. Do not run the notebook task or approval cells for the same turn.
- **Browser-owned turn:** create or select a browser conversation, submit the task, and decide the complete action group there. Use that browser session identifier for later replay; do not run the notebook task or approval cells for the same turn.

Submit this task through the selected interface:

```text
Build the synthetic target-condition cohort for concept 201826 with the repository-verified cohort Skill. Read the tables in input, require age 18 or older, apply an aggregate count floor of 20, write cohort-summary.json, and report aggregate quality checks without row-level values.
```

Review every member of the pending OpenHands action set. Select **Allow all once** only when every command, path, and output matches the request. Exercise **Reject all** on a separate synthetic proposal. OpenHands currently returns a grouped action set, so Heartwood applies one decision to the complete displayed group rather than implying that members can be approved independently.

The expected result reports 24 source participants, 39 source condition rows, 20 cohort participants, 35 cohort condition rows, passing integrity checks, and no row-level values. A matching result validates the integration workflow, not biomedical correctness for another dataset.

This is a capability check for the selected model, not a guarantee that every model will produce the required action plan. If a model does not propose the repository-verified workflow or create the expected aggregate artifact, reject unexpected actions and record the model as not passing this acceptance task.

Use **Activity & audit** in the browser or `/replay` in the terminal to inspect route decisions, proposals, the grouped decision, tool outcomes, and errors. Action confirmation defaults to **Ask Every Time**. A deployment may permit **Auto-Approve Low Risk**, but medium-, high-, and unknown-risk groups still require review.

## 6. Verify the Shared Session

Wait until the active turn is idle, then replay and export the same session from a terminal in the project. Replace `terra-demo` when the browser created a different identifier:

```bash
heartwood --session-id terra-demo replay
heartwood --session-id terra-demo audit export \
  --output terra-demo-audit.jsonl
```

The notebook uses the same project without a workspace argument:

```python
from pathlib import Path

from heartwood.notebook import NotebookSession, jupyter_proxy_url

project_root = Path.cwd().resolve()
session = NotebookSession(session_id="terra-demo")
view = session.replay()
assert session.project.root == project_root
print(view.event_count)
print(jupyter_proxy_url(port=8767))
```

The terminal, notebook, and browser should report the same persisted events. Use one active writer for a session; independent processes writing the same file-backed session concurrently are not supported.

## Run a Local Model on Terra

### Portable CPU Path

The portable Terra image uses llama.cpp on CPU. Select the reviewed recommendation or provide another supported Hugging Face repository:

```bash
heartwood models download qwen25-7b-instruct-q4_k_m
# Or inspect and download another repository:
heartwood models inspect <owner/model>
heartwood models download <owner/model>
heartwood launch --web
```

Heartwood resolves the source to an immutable revision, displays expected storage and memory, downloads into the current project's `.heartwood/models/` directory, verifies the artifact, and persists the selection. A full OpenHands turn on a 7-billion-parameter CPU model can take several minutes. Attaching a GPU does not accelerate the portable image's llama.cpp runtime.

### NVIDIA GPU Path

The NVIDIA Terra image adds the GPU inference runtime but still contains no model weights. On a T4-class environment, use the 4-bit coding recommendation:

```bash
heartwood models download qwen25-coder-7b-instruct-awq-vllm
heartwood launch --web
```

Larger GPUs can use `qwen25-7b-instruct-vllm`. The reviewed recommendations use a 32,768-token context window. Before startup, Heartwood reports estimated and observed RAM and GPU memory, verifies the runtime, initializes the attached GPU, and stops with a diagnostic when the image, driver, model, or available memory is incompatible. Warnings are conservative; reduce model size or context rather than ignoring repeated out-of-memory failures.

Model download and model startup are separate operations. Downloads report transferred bytes and remain on the persistent disk. Startup can take several minutes while model files are loaded; the terminal reports elapsed time until the server is ready.

## Troubleshoot the Terra Workflow

### Jupyter Opens a 404 Page

Wait until the Cloud Environment finishes starting, then use the workspace terminal's **Jupyter Notebook** link. If the normal Jupyter file browser still fails, inspect the Cloud Environment error before changing Heartwood settings. The Heartwood image preserves Terra's Jupyter entrypoint; reinstalling packages inside the container is not a repair path.

### The Heartwood Browser Route Returns 404

Confirm that `heartwood serve` or `heartwood launch --web` is still running in the project terminal. Generate the link from the tutorial notebook instead of shortening it to `/proxy/8767/`. If the proxy remains unavailable, continue in the CLI and record the proxy failure separately.

### The Interface Opens but the Model Does Not Respond

Run `heartwood doctor` from the same project directory. A downloaded model is not a running model; start it with `heartwood launch` or `heartwood launch --web` and keep that process alive. For a hosted connection, refresh the model list and verify the selected connection without printing credentials:

```bash
heartwood models list
heartwood doctor
```

For a Heartwood-managed local model, inspect `.heartwood/logs/local-model.log`. If port `8767` or the model-server port is already occupied, stop the older Heartwood process before restarting; do not run two launchers for one project.

### The GPU Is Not Used

Confirm that the Cloud Environment uses the `-terra-gpu-nvidia` image and has an attached NVIDIA GPU. The portable image always uses its CPU runtime. Run `nvidia-smi` and `heartwood doctor`; if the GPU is visible but startup fails, keep the launcher diagnostic and choose a smaller recommended model when memory is insufficient.

Do not expect automatic CPU fallback from the NVIDIA runtime after a CUDA or GPU-memory failure. Use a smaller GPU recommendation, or replace the environment with the portable image and explicitly choose a CPU model.

### The Browser, Notebook, and CLI Show Different Projects

Run every process from the directory containing the analysis and notebook. `pwd` in the terminal and `Path.cwd()` in the notebook must identify the same directory. Starting a service from `/home/jupyter` while the notebook lives in `/home/jupyter/heartwood-demo` creates two intentional Heartwood projects.

### Storage Disappears After Environment Replacement

Only files on the retained persistent disk survive environment replacement. Keep the project under `/home/jupyter`, verify it before deleting compute, and copy important deliverables to workspace storage. Retaining a disk is not a substitute for a project backup policy.

### A Model Download Fails

Check free space with `df -h /home/jupyter`, then rerun `heartwood models download <model-id>`. Heartwood downloads into the current project's `.heartwood/models/` directory. A private or gated Hugging Face repository also requires its access terms and authentication to be completed before download.

### Terra Rejects the Image Before Startup

Use the exact `-terra` or `-terra-gpu-nvidia` release tag rather than the generic multi-platform tag. Terra Leonardo requires the single-platform Docker schema-2 manifest published for Terra. A manifest auto-detection error occurs before Heartwood or Jupyter starts and cannot be repaired from inside the environment.

## Record Validation Evidence

Record synthetic evidence only:

- Heartwood image and Terra base image digests;
- machine shape, disk size, startup time, and pause/resume behavior;
- Jupyter route, Heartwood kernel, and browser proxy result;
- selected non-secret model profile and route-policy decision;
- optional local artifact identifier and digest;
- one allowed and one rejected action group;
- matching terminal, browser, and notebook replay results;
- scrubbed audit export location;
- observed platform identity and network controls.

The current release is implemented and CI-validated for Terra. [Platform Support and Validation](platform-support.md#terra) records which parts have also completed synthetic live validation. A real Terra workspace validation is still distinct from institutional approval. Do not introduce controlled data until the exact image, model route, credentials, project storage, network path, and intended use have passed institutional review.

## Image and Deployment Details

The release extends the pinned Terra Jupyter Python base recorded in `images/platforms.toml`. It preserves the `jupyter` user, persistent home, notebook server, Heartwood kernel, entrypoint, and Leonardo proxy behavior.

The public Terra tag is a `linux/amd64` Docker schema-2 manifest with media type `application/vnd.docker.distribution.manifest.v2+json`. Terra Leonardo image auto-detection rejects an Open Container Initiative index, so Terra tags intentionally differ from the generic multi-platform tag. `edge-terra` follows the latest validated `main` build and is only for development testing.

Record both the Heartwood image and Terra base image digests. Passing image and proxy checks establishes software compatibility, not authorization for workspace data, a model provider, or controlled-data use.

## Terra References

- [Terra custom cloud environment tutorial](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks)
- [Starting and customizing a Jupyter app](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app)
- [Persistent disk setup](https://support.terra.bio/hc/en-us/articles/7131848736027-How-to-set-up-persistent-disk-storage-for-your-analysis-app)
- [Terra architecture and persistent disks](https://support.terra.bio/hc/en-us/articles/360058163311-Terra-architecture-where-your-data-and-tools-live)
- [Accessing workspace-bucket data from a notebook](https://support.terra.bio/hc/en-us/articles/360046617372-Accessing-data-from-the-workspace-Bucket-in-a-notebook)
- [DataBiosphere Terra Docker images](https://github.com/DataBiosphere/terra-docker)
