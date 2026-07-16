<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Get Started with Heartwood

This guide takes you from choosing an installation path to completing the first reviewed Heartwood task. You will create one project, connect one model, choose an interface, and learn where Heartwood keeps its state.

!!! warning "Begin with non-sensitive files"

    Learn and validate the workflow with synthetic or non-sensitive data. A working Heartwood installation does not by itself authorize a model, machine, or research platform for controlled data.

The complete first-use path is:

1. choose one way to run Heartwood;
2. enter the directory the agent may modify;
3. connect an authorized model;
4. open the terminal, browser, or notebook interface;
5. submit and review a bounded task.

## Step 1: Choose How to Run Heartwood

Heartwood offers the same project and conversation experience through several installation paths. Choose one; you do not need both a container and a native installation.

### Use the Container

Use the container for a workstation or general-purpose server with Docker. It is the recommended first-use path because the image already contains Heartwood, its browser interface, and the software needed to run a supported CPU model. It does not contain model weights or credentials.

Follow [Run Heartwood in a Container](container-images.md#quick-start-the-browser) to start the browser or terminal with the current directory mounted as the project.

### Install the Command

Use the native installation when Docker is unavailable or Heartwood must run directly inside an existing managed environment. It installs the `heartwood` command directly in that environment. This is the normal path for Stanford Carina and a useful path for contributors.

Choose a published version from [Heartwood Releases](https://github.com/SchmiedmayerLab/heartwood/releases) and use the native installer attached to that release, then follow any platform-specific instructions. The [Carina guide](carina-cli.md) provides the complete managed-environment example.

### Use a Managed Platform Image

Some notebook platforms require their own base image, user, storage, and routing behavior. Terra therefore uses a Terra-derived Heartwood image rather than the generic image. Follow [Use Heartwood on Terra](terra-jupyter-demo.md) without installing another copy inside the notebook environment.

[Choose Where to Run Heartwood](platforms.md) explains these options in more detail.

## Step 2: Create or Open a Project

The project is the directory Heartwood may work on. Create one or enter an existing analysis directory before starting Heartwood:

```bash
mkdir analysis-project
cd analysis-project
heartwood
```

Heartwood uses that exact directory as the project. Starting Heartwood from a different directory opens a different project.

Heartwood saves project setup and conversations in a private `.heartwood/` directory. You normally do not need to open or edit it. See [Project Files and State](project-state.md) when you need storage or migration details.

!!! tip "Confirm the boundary before continuing"

    Run `pwd` in a terminal. Heartwood may work in that directory and its subdirectories. If the path is too broad, create a narrower project directory before setup.

## Step 3: Choose a Model

The first run asks where the model should run. Choose the simplest authorized option available in your environment:

1. Select **Research environment** when your platform or institution already provides models.
2. Select **On this device** to use an existing local service or let Heartwood prepare a supported local model.
3. Select **OpenAI**, **Anthropic**, or **Custom API** only when that route is permitted for the project data.

Heartwood then displays the models available from that source. Provider tokens are entered privately for the current process and are not written into project configuration.

See [Connect a Model](model-connections.md) for every connection type. See [Run a Model Locally](getting-started-offline.md) before downloading model weights or choosing CPU or GPU inference.

## Step 4: Choose an Interface

| Interface | Start it with | Use it for |
|---|---|---|
| Interactive terminal | `heartwood` | Initial setup, reliable remote use, conversation, and action review. |
| Browser | `heartwood serve` | Visual setup, conversation, grouped action review, and audit inspection. |
| Browser with local model | `heartwood launch --web` | Starting and supervising a downloaded model while the browser is open. |
| Notebook | `NotebookSession` from a notebook in the project | Detection, task submission, grouped approval, replay, and audit beside an analysis. |

Use `heartwood launch` instead of bare `heartwood` when a downloaded local model must be started for the terminal. The notebook reuses a configured project and running model connection; it does not replace initial setup or local-model supervision.

[Browser and Notebooks](web-interface.md) explains visual setup, local-model progress, Jupyter routing, and shared sessions.

## Step 5: Ask for the First Task

After setup, Heartwood opens the conversation when the selected model is already available. When setup downloads a local model, it instead tells you to start the model and terminal together:

```bash
heartwood launch
```

Enter a specific request after the conversation opens. State the files Heartwood should use, the expected result, and any restrictions that matter. For example:

```text
Inspect the CSV files in input, summarize missing values by column, and write the aggregate result to missingness-summary.csv. Do not include row-level values.
```

When Heartwood proposes commands or file changes, review the complete action set. Choose **Allow all once** only when every action is appropriate; choose **Reject all** when any action is unnecessary or unclear.

Exiting Heartwood does not remove the project setup or conversation. Start `heartwood` from the same directory to continue.

## Check Readiness at Any Time

Run the read-only diagnostic whenever the next step is unclear:

```bash
heartwood doctor
```

The result tells you whether setup is needed, a downloaded model needs to be started, the project is ready, or a configuration problem needs attention.

## Continue from Here

| Next question | Continue with |
|---|---|
| How do I write effective requests and review actions? | [Work with the Agent](using-heartwood.md) |
| How do I use the browser or a notebook? | [Browser and Notebooks](web-interface.md) |
| How do model connections and credentials work? | [Connect a Model](model-connections.md) |
| How do I download and run a model locally? | [Run a Model Locally](getting-started-offline.md) |
| What should I do when a check fails? | [Troubleshooting](troubleshooting.md) |
