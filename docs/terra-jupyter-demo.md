<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Terra-Style Jupyter Demo

This runbook demonstrates the current Heartwood stack from a Terra-like Jupyter environment using only synthetic data. It covers the CLI, notebook API, packaged researcher web UI, gateway session routes, local smoke model path, OpenHands-backed bounded tool execution, audit export, and reviewer packet generation. The repository CI validates the same proxy mechanics with a local stripped `jupyter-server-proxy` smoke; a real Terra workspace is still required before claiming platform identity binding or controlled-data access.

## Image Selection

Use `ghcr.io/schmiedmayerlab/heartwood:edge-smoke` when the demo must include the tiny bundled model artifact and the full offline smoke path. Use `ghcr.io/schmiedmayerlab/heartwood:edge` when demonstrating the UI, CLI, notebook API, provider route configuration, and gateway behavior without bundled model weights. In Terra, configure the Cloud Environment to use the selected image directly as the notebook container; do not run nested Docker inside the workspace.

## Runnable Notebook

The companion notebook [terra-jupyter-demo.ipynb](terra-jupyter-demo.ipynb) contains the synthetic notebook cells for the same demo path. Use it inside the Heartwood image after the workspace starts to calculate the Jupyter proxy URL, run detection through `NotebookSession`, submit the synthetic workflow, inspect approval controls and activity, export the scrubbed audit log, and compare the result with CLI replay and the proxied researcher web UI.

## Terminal Smoke

Run the offline stack smoke from a Jupyter terminal inside the `edge-smoke` image:

```bash
bash images/generic/scripts/offline_stack_smoke.sh
```

The smoke starts the local `llama-cpp-cpu` profile, runs detection, records an explicit model approval, invokes `heartwood run --local-model`, starts the gateway-managed OpenHands process, executes the bounded synthetic tool path, exports a scrubbed audit log, writes a synthetic reviewer packet, and runs the packaged Terra-style Jupyter proxy smoke. The proxy smoke starts `heartwood serve`, exposes a local `/user/synthetic/proxy/<port>/` route, strips that prefix before forwarding to the gateway, verifies static assets, command submission, event replay, Server-Sent Events, and checks the notebook API in the same workspace.

## Researcher Web UI

Start the gateway-served web UI from a Jupyter terminal:

```bash
export HEARTWOOD_WORKSPACE=/home/jupyter/heartwood-workspace
export HEARTWOOD_WEB_HOST=127.0.0.1
export HEARTWOOD_WEB_PORT=8767
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
for approval in run.approvals:
    print(approval.target_type, approval.target_id, approval.status)
```

## Demo Evidence

A complete synthetic demo should show the terminal smoke output, the proxied web UI loading through the notebook route, the notebook API returning the same session activity, the CLI replay showing the persisted session events, and the generated audit/reviewer artifacts. Current CI verifies the local offline stack, the packaged UI, preserved-prefix gateway routes, stripped Jupyter-style routes, replay, and Server-Sent Events. A real Terra validation pass must still confirm Terra image launch, Leonardo proxy behavior, inherited identity, and any platform-specific model or data access controls.
