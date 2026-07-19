<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Get Started

% TODO: This might already sounds too technical, can we have a nice layout here to define the tasks. Start an interface sounds strange; someone non-technical might not understand that. Be clear what this means ...
This guide takes you from an empty project to the first reviewed Heartwood task. You will choose one installation, open one project directory, start an interface, and connect a model in that interface.

% TODO: This sounds too abstract; what should that define here. Maybe we make this a bit more approchable?
!!! warning "Begin with non-sensitive files"

    A working Heartwood installation does not authorize a model, machine, or research platform for controlled data. Learn the workflow with synthetic or non-sensitive files first.

## 1. Choose How Heartwood Runs

Use the [container](container-images.md) for the shortest workstation setup, the [Terra image](terra-jupyter-demo.md) in a Terra workspace, or the [Carina installation](carina-cli.md) on Stanford Carina. A general Linux server can use the [native terminal installation](installation.md). Follow operator instructions in another managed environment.

Choose one route. You do not need both a container and a native installation for the same project. [Choose an Environment](platforms.md) explains the tradeoffs.

## 2. Open a Project

% TODO: be a bit celarer here, make this approachable. Probably the first time someone interacts with the platform ...
The project is the directory Heartwood may inspect and modify. Enter that directory before starting Heartwood:

% TODO: Maybe even explain this in two steps, the first one maybe even appraochable to link to the "basics of using a terminal" or something to make this as easy as possible. We should consider the same things for the installation pathways ...
```bash
mkdir analysis-project
cd analysis-project
heartwood
```

Starting Heartwood from another directory opens another project.
% TODO: No one knows what this means?!
Run `pwd` first when the boundary matters.

Heartwood keeps private configuration, sessions, downloaded models, and audit data with the project.
% TODO: For someone who doesn't even know what that means, this is useless ... make this and other elemnts here more approachable ...
You do not need to pass workspace or state paths.
[Projects and Persistent State](project-state.md) explains the layout and backup behavior.

## 3. Choose an Interface

| Interface | Start command | Use it for |
|---|---|---|
| Terminal | `heartwood` | Guided setup, conversation, action review, and reliable remote use |
% TODO: We should re-think some of the commands to make this even easier to work with ...
| Browser | `heartwood serve` | Visual guided setup, conversation, action review, Skills, and audit activity |
| Terminal with downloaded model | `heartwood launch` | Start the local model and open the conversation |
| Browser with downloaded model | `heartwood launch --web` | Start the local model and browser together |
% TODO: NOtebook session is not a command; this feels a bit off ... how do me make this more approachable ...
| Notebook | `NotebookSession` | Continue a project whose model and credential are already available to the notebook process |

% TODO: Well, this doens't really help; half of the commands above don't enable this at all ... we whould rather provide a nice overview of the options, and then clear setups on how to start this, maybe even embedding or linking ot other documentation elements ....
Open `http://127.0.0.1:8767/` after `heartwood serve` on a local machine. Managed platforms may provide an authenticated proxy URL instead.

% TODO: Well, this is not really helpful; maybe a matrix might help here to easily understand what platform supports what thing?
Generic native release installations provide the terminal and Heartwood's Python notebook API, but they do not register a Jupyter kernel or include the built browser application. Notebook use there requires operator integration. Containers, Terra images, and source checkouts with built web assets provide the browser.

## 4. Connect a Model

An unconfigured terminal or browser opens guided setup. Choose the simplest authorized source available:

% This migtht be very helpful, good to have a dedicated Stanford option here, but we should maybe rather highlight this as one of the possible options for "other" OpenAI-like API interface?
- **On this device** for an existing local service or a model that Heartwood downloads and runs.
- **OpenAI** when the project is authorized to use OpenAI.
- **Anthropic** when the project is authorized to use Anthropic.
- **Stanford AI API Gateway** when that Stanford-managed route is authorized.

% TODO: We should change that, the token should be saved in the .heartwood directory; maybe in a way that only the heartwood process can read it? Is there some secure way to enable that?
Heartwood asks the selected service for its available models whenever possible. Enter a hosted-provider token in the same terminal or browser process that will run the conversation. The token remains only in that process and is not saved in project configuration.

% TODO: 1. Why not? 2. Is that really relenvant here? Maybe also a matrix of supported elmenets should be good here ... generally, make sure that all these sectiosn are here to provide an overview and make this apprachable. We should highlight one easy path and make sure that one can follow this getting started guide in one swoop withouth having to go to different pages.
The notebook bridge does not offer interactive token setup. Use it with a running local model or with a credential supplied to the notebook kernel by the deployment. See [Choose a Model](model-connections.md) for connection details and [Run a Model Locally](getting-started-offline.md) before downloading model files.

## 5. Ask for a Bounded Result

% TODO: How?! This is all not really approachable, we don't even document one golden path in something that's called "getting started" guide ... maybe show the coolest and easiest path that works across all platofrms with a capable local model that is small enough to do something useful but not crazy so it takes too long to download? That might be the most self-contained pathway we can demonstrate?
State the input, expected output, and important restrictions. For example:

% TODO: What CSV files? If there is nothing then this fails ... bad example; we need to do something very approachable ... it needs to be a success on the first try ....
```text
Inspect the CSV files in input, summarize missing values by column, and write the aggregate result to missingness-summary.csv. Do not include row-level values.
```

% TODO: Make this clearer and more interactive; demonstrate the overall elments that heartwood provies? 
When Heartwood proposes actions, review every command, operation, path, and output in the displayed group. Choose **Allow all once** only when the complete group is appropriate. Choose **Reject all** when any member is unnecessary or unclear.

Exiting Heartwood keeps the project and session. Start it again from the same directory to continue.

% TODO: Why is this the last thing, shouldn't this be the first thing before we show the usage of the main interface? This is not a good and appraochable guide and order ...
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
