<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Get Started

This guide takes you from an empty project to the first reviewed Heartwood task. You will choose one installation, open one project directory, start an interface, and connect a model in that interface.

!!! warning "Begin with non-sensitive files"

    A working Heartwood installation does not authorize a model, machine, or research platform for controlled data. Learn the workflow with synthetic or non-sensitive files first.

## 1. Choose How Heartwood Runs

Use the [container](container-images.md) for the shortest workstation setup, the [Terra image](terra-jupyter-demo.md) in a Terra workspace, or the [Carina installation](carina-cli.md) on Stanford Carina. A general Linux server can use the [native terminal installation](installation.md). Follow operator instructions in another managed environment.

Choose one route. You do not need both a container and a native installation for the same project. [Choose an Environment](platforms.md) explains the tradeoffs.

## 2. Open a Project

The project is the directory Heartwood may inspect and modify. Enter that directory before starting Heartwood:

```bash
mkdir analysis-project
cd analysis-project
heartwood
```

Starting Heartwood from another directory opens another project. Run `pwd` first when the boundary matters.

Heartwood keeps private configuration, sessions, downloaded models, and audit data with the project. You do not need to pass workspace or state paths. [Projects and Persistent State](project-state.md) explains the layout and backup behavior.

## 3. Choose an Interface

| Interface | Start command | Use it for |
|---|---|---|
| Terminal | `heartwood` | Guided setup, conversation, action review, and reliable remote use |
| Browser | `heartwood serve` | Visual guided setup, conversation, action review, Skills, and audit activity |
| Terminal with downloaded model | `heartwood launch` | Start the local model and open the conversation |
| Browser with downloaded model | `heartwood launch --web` | Start the local model and browser together |
| Notebook | `NotebookSession` | Continue a project whose model and credential are already available to the notebook process |

Open `http://127.0.0.1:8767/` after `heartwood serve` on a local machine. Managed platforms may provide an authenticated proxy URL instead.

Generic native release installations provide the terminal and Heartwood's Python notebook API, but they do not register a Jupyter kernel or include the built browser application. Notebook use there requires operator integration. Containers, Terra images, and source checkouts with built web assets provide the browser.

## 4. Connect a Model

An unconfigured terminal or browser opens guided setup. Choose the simplest authorized source available:

- **On this device** for an existing local service or a model that Heartwood downloads and runs.
- **OpenAI** when the project is authorized to use OpenAI.
- **Anthropic** when the project is authorized to use Anthropic.
- **Stanford AI API Gateway** when that Stanford-managed route is authorized.

Heartwood asks the selected service for its available models whenever possible. Enter a hosted-provider token in the same terminal or browser process that will run the conversation. The token remains only in that process and is not saved in project configuration.

The notebook bridge does not offer interactive token setup. Use it with a running local model or with a credential supplied to the notebook kernel by the deployment. See [Choose a Model](model-connections.md) for connection details and [Run a Model Locally](getting-started-offline.md) before downloading model files.

## 5. Ask for a Bounded Result

State the input, expected output, and important restrictions. For example:

```text
Inspect the CSV files in input, summarize missing values by column, and write the aggregate result to missingness-summary.csv. Do not include row-level values.
```

When Heartwood proposes actions, review every command, operation, path, and output in the displayed group. Choose **Allow all once** only when the complete group is appropriate. Choose **Reject all** when any member is unnecessary or unclear.

Exiting Heartwood keeps the project and session. Start it again from the same directory to continue.

## Check Readiness

Run the read-only diagnostic when the next step is unclear:

```bash
heartwood doctor
```

The result identifies incomplete setup, a local model that needs to be started, or a configuration problem. [Troubleshooting](troubleshooting.md) explains each readiness state.

## Next Steps

- Learn the daily workflow in [Work with Heartwood](using-heartwood.md).
- Compare the [terminal, browser, and notebook](web-interface.md).
- Follow the complete [Terra](terra-jupyter-demo.md) or [Carina](carina-cli.md) environment guide.
