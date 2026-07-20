<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood on Terra

[Terra](https://terra.bio/) provides cloud workspaces, Jupyter applications, compute, storage, and access controls for biomedical research.
Heartwood extends Terra's Jupyter Python image so the normal notebook routes and persistent disk remain available while terminal, browser, notebook, OpenHands, Skills, and Heartwood-managed inference use one project.

This guide changes cloud compute and can incur charges.
Review the cost shown by Terra and begin with synthetic or non-sensitive data.

## Before You Begin

You need access to a Terra workspace and permission to create or replace its Jupyter Cloud Environment.
Terra supports custom images from GitHub Container Registry and requires them to derive from a Terra base image; see Terra's [custom Jupyter environment guide](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks).

## Choose the Image and Compute

In the workspace, open the Jupyter Cloud Environment settings, select **Customize**, and choose **Custom Environment** under application configuration.
Enter one image:

| Workload | Image | Practical Starting Point |
|---|---|---|
| Hosted model | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra` | 8 CPUs, 30 GB RAM, 50 GB persistent disk |
| CPU model run by Heartwood | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra` | 8 CPUs, 32 GB RAM, at least 75 GB persistent disk |
| Heartwood-managed vLLM model on NVIDIA GPU | `ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3-terra-gpu-nvidia` | 8 CPUs, 48 GB RAM, one T4-class GPU or better, at least 75 GB persistent disk |

These are tutorial starting points rather than universal requirements.
A hosted model is the shortest first run; CPU inference managed by Heartwood is useful for portability but can be slow.
For a larger Heartwood-managed model, create the environment with a retained persistent disk, inspect the model plan in Heartwood, and then resize compute or switch to the GPU image before downloading when the recommendation exceeds the starting point.

Terra recommends starting with the minimum resources and scaling when needed; changing compute can recreate the environment, so retain the persistent disk and copy valuable outputs to workspace storage.
See [Starting and Customizing Your Jupyter App](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app).

## Create the Environment

Select **Create** or **Update** and wait for Jupyter to become ready.
Open Jupyter, then open **File → New → Terminal**.

Verify the release:

```bash
heartwood --version
```

Do not install another Heartwood copy into the notebook environment.
The custom image already provides the tested application and kernel while preserving Terra's base-image behavior.

## Create a Project Directory

Terra preserves files written under `/home/jupyter` when the persistent disk is retained.
Create a dedicated child directory rather than using `/home/jupyter` itself:

```bash
mkdir -p /home/jupyter/heartwood-project
cd /home/jupyter/heartwood-project
heartwood doctor
```

The current directory is the Heartwood project for the terminal, browser, and notebooks launched there.
Terra documents persistent-disk behavior and deletion precautions in its [Cloud Environment FAQ](https://support.terra.bio/hc/en-us/articles/360057425291-Cloud-Environment-FAQs).

## Start the Terminal

```bash
heartwood
```

Choose a model route permitted for the workspace.
OpenAI and Anthropic appear in the baseline adapter; deployment policy and institutional agreements still determine whether they may receive the intended data.
**Other compatible service** records an explicitly entered OpenAI-compatible endpoint in the project policy, but does not make that endpoint institutionally authorized.
To run inference in the Terra environment, choose **Run with Heartwood** and review the model and resource plan before download.

## Open the Browser

From the same project:

```bash
heartwood --interface web
```

Keep the terminal running and open the authenticated Jupyter proxy path printed by Heartwood on the same Terra Jupyter host.
The route contains runtime-specific components and must not be shortened to `/proxy/8767/` or reused from an old environment.
The tutorial notebook can render the same verified path as a clickable link when the kernel exposes Terra's runtime identifiers.

The browser uses the same `.heartwood/` state and selected model as the terminal.
If Terra has not supplied enough proxy identity information, Heartwood withholds a guessed URL; use the terminal or notebook interface instead of constructing a shorter path manually.

## Use a Notebook

Create a Python notebook in the project directory and select the **Python 3 (Heartwood)** kernel.
Follow [Use Heartwood From a Notebook](../use/notebooks.md) to inspect readiness, submit a task, review a grouped action set, and export the audit record.
The [downloadable Terra notebook](../assets/examples/terra-heartwood.ipynb) provides an output-free synthetic starting point using the same API.

The notebook process current directory determines its project.
Use `Path.cwd()` to verify the boundary before creating `NotebookSession()`.

## Preserve Results and Stop Compute

Save project files under the persistent disk and copy durable results to the workspace bucket according to the research workflow.
Exit Heartwood, stop or pause the Cloud Environment when finished, and review whether the persistent disk should remain.

Terra charges can continue for retained resources, and deleting a persistent disk removes `.heartwood/` and project files stored only there.

## Troubleshooting Terra

- A Jupyter **404** at the root can be normal when the complete notebook route was not used; open Jupyter from Terra rather than a guessed host path.
- If the Heartwood page returns **404**, start it from the project and use the exact proxy URL printed after startup.
- If the model is slow or fails to load, compare the selected model plan with attached RAM, GPU memory, and persistent-disk space.
- If Terra rejects the image during auto-detection, confirm that the tag ends in `-terra` or `-terra-gpu-nvidia`; those tags are published as single-platform Docker schema-2 manifests for Leonardo compatibility.
- Run `heartwood doctor` for stable `HW-TERRA-*` recovery guidance.
