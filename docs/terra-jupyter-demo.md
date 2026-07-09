<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Terra-Style Jupyter Demo

This runbook demonstrates the current Heartwood stack from a Terra-derived Jupyter image using only synthetic data. It covers the CLI, notebook API, packaged researcher web UI, gateway session routes, local smoke model path, OpenHands-backed bounded tool execution, audit export, and reviewer packet generation. Repository CI validates the same platform Dockerfile through a Terra-compatible notebook base with runtime network disabled, and the main-branch image workflow publishes the real Terra-derived tags; a real Terra workspace is still required before claiming Terra workspace launch support, platform identity binding, or controlled-data access.

## Current Status

Use `ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke` when the Terra demo must include the tiny bundled model artifact and the full offline smoke path. Use `ghcr.io/schmiedmayerlab/heartwood:edge-terra` when demonstrating the UI, CLI, notebook API, provider route configuration, and gateway behavior without bundled model weights. These tags publish automatically from `main`, and the image workflow verifies the public GHCR pull path through unauthenticated registry inspection before it succeeds.

Terra's current custom-environment guidance says custom Jupyter images should be based on a Terra Jupyter Notebook base image or a project-specific image, and the `DataBiosphere/terra-docker` repository states that notebook custom images need to use a Terra base image to work with Terra's notebook service. The Heartwood Terra image derives from `us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6`, installs Heartwood under `/opt/heartwood`, registers a `heartwood` Jupyter kernel, and keeps the generic `edge`, `edge-smoke`, and `edge-providers` tags out of the Terra custom-environment path.

Relevant Terra references reviewed for this runbook:

- [Standardizing A Custom RStudio Or Jupyter Environment](https://support.terra.bio/hc/en-us/articles/5713716832027-Standardizing-a-custom-RStudio-or-Jupyter-environment)
- [Docker Tutorial: Custom Cloud Environments For Jupyter Notebooks](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks)
- [DataBiosphere/terra-docker Terra Base Images](https://github.com/DataBiosphere/terra-docker#terra-base-images)
- [Getting Started With GPUs In A Cloud Environment](https://support.terra.bio/hc/en-us/articles/4403006001947-Getting-started-with-GPUs-in-a-Cloud-Environment)

## Terra Image Strategy

The Terra image is a platform-specific notebook image that extends the current Terra Jupyter Python base, installs the Heartwood runtime into that base, keeps Jupyter on Terra's expected notebook service path, and starts the Heartwood gateway/web UI on a loopback port such as `8767` for access through the notebook proxy. Pull-request CI builds `edge-terra-smoke-ci` from a lightweight Terra-compatible base through the same platform Dockerfile, runs the platform image smoke, and runs the full offline stack smoke with runtime network disabled. The main-branch publish workflow builds and publishes `edge-terra` and `edge-terra-smoke` from the real Terra base after freeing runner disk space.

The generic image still remains the portable source runtime for local Docker reproducibility. The Terra image carries `README.md`, `ACRONYMS.md`, `docs/`, and `design/` under `/opt/heartwood` so a packaged runtime contains the tutorial notebook and design material even without a repository checkout.

## Runnable Notebook

The companion notebook [terra-jupyter-demo.ipynb](terra-jupyter-demo.ipynb) contains the synthetic notebook cells for the same demo path. Use it inside the Heartwood image after the workspace starts to calculate the Jupyter proxy URL, run detection through `NotebookSession`, submit the synthetic workflow, inspect approval controls and activity, export the scrubbed audit log, and compare the result with CLI replay and the proxied researcher web UI. In a packaged image, the notebook is available at `/opt/heartwood/docs/terra-jupyter-demo.ipynb`; copy it into the workspace home directory if Terra's notebook file browser only starts from `/home/jupyter`.

## Local Terminal Smoke

Run the offline stack smoke from a terminal inside the `edge-terra-smoke` image:

```bash
cd /opt/heartwood
bash images/generic/scripts/offline_stack_smoke.sh
```

The smoke starts the local `llama-cpp-cpu` profile, runs detection, records an explicit model approval, invokes `heartwood run --local-model`, starts the gateway-managed OpenHands process, executes the bounded synthetic tool path, exports a scrubbed audit log, writes a synthetic reviewer packet, and runs the packaged Terra-style Jupyter proxy smoke. The proxy smoke starts `heartwood serve`, exposes a local `/user/synthetic/proxy/<port>/` route, strips that prefix before forwarding to the gateway, verifies static assets, command submission, event replay, Server-Sent Events, and checks the notebook API in the same workspace.

## Researcher Web UI

Start the gateway-served web UI from a Jupyter terminal:

```bash
export HEARTWOOD_WORKSPACE=/home/jupyter/heartwood-workspace
export HEARTWOOD_WEB_HOST=127.0.0.1
export HEARTWOOD_WEB_PORT=8767
cd /opt/heartwood
bash images/generic/scripts/start_web_ui.sh
```

Open the notebook proxy URL for port `8767`. In Jupyter environments that expose `/user/<name>/proxy/<port>/` and strip that prefix before forwarding, keep `HEARTWOOD_WEB_BASE_PATH` unset or set to `/`. In proxy environments that preserve the prefix before forwarding, set `HEARTWOOD_WEB_BASE_PATH=/proxy/8767/` before starting the launcher.

## Notebook API

Run the same session contract from a notebook cell:

```python
from pathlib import Path

from heartwood.notebook import NotebookSession, jupyter_proxy_url

workspace = Path("/home/jupyter/heartwood-workspace/sessions")
session = NotebookSession(workspace=workspace, session_id="terra-demo")

print(jupyter_proxy_url(port=8767))
detection = session.detect()
detection.activity[-1]
```

Then submit a synthetic run through the notebook API:

```python
run = session.run("run the synthetic workflow")
run.policy_status[-1]
```

If the run returns approval controls, approve only the synthetic targets shown in the notebook view model:

```python
for approval in run.approval_controls:
    print(approval.target_type, approval.target_id, approval.decision)
```

## Live Terra End-To-End Trial

Use this checklist after `ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke` has been published by the main-branch image workflow. The GHCR package must be public before the Terra Cloud Environment is created; verify that an unauthenticated Docker client can inspect the tag from outside the workflow login with `DOCKER_CONFIG="$(mktemp -d)" docker manifest inspect ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke`.

1. Create or select a synthetic-only Terra workspace; do not use controlled data for this validation.
2. Create a Jupyter Cloud Environment with `ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke`, a Standard VM, no GPU for the current `llama-cpp-cpu` profile, and enough disk for the image plus `/home/jupyter/heartwood-workspace`.
3. Open a Jupyter terminal and run `cd /opt/heartwood && bash images/generic/scripts/offline_stack_smoke.sh`.
4. Start the web UI with `export HEARTWOOD_WORKSPACE=/home/jupyter/heartwood-workspace/sessions`, `export HEARTWOOD_WEB_HOST=127.0.0.1`, `export HEARTWOOD_WEB_PORT=8767`, then `cd /opt/heartwood && bash images/generic/scripts/start_web_ui.sh`.
5. In a notebook cell, run `from heartwood.notebook import jupyter_proxy_url; jupyter_proxy_url(port=8767)` and open the returned URL.
6. Submit a synthetic prompt in the researcher web UI chat form, confirm that the event stream updates through the proxy, and compare the same session in the notebook API and `heartwood --workspace /home/jupyter/heartwood-workspace/sessions --session-id terra-demo replay`.
7. Save the terminal smoke output, gateway/web UI URL shape, one web UI chat interaction, notebook replay count, scrubbed audit export path, reviewer packet path, Terra image digest, Terra base image digest, custom image digest, VM shape, and whether any Terra proxy headers or path behavior differ from the local stripped-proxy smoke.

## Demo Evidence

A complete synthetic demo should show the terminal smoke output, the proxied web UI loading through the notebook route, a chat-like synthetic interaction in the web UI, the notebook API returning the same session activity, the CLI replay showing the persisted session events, and the generated audit/reviewer artifacts. Current CI verifies the platform Dockerfile build through a Terra-compatible CI base, local offline stack, packaged UI, preserved-prefix gateway routes, stripped Jupyter-style routes, replay, and Server-Sent Events. A real Terra validation pass must still confirm Terra workspace launch, Leonardo proxy behavior, inherited identity, and any platform-specific model or data access controls.
