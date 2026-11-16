<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Your First Heartwood Project

This guide goes from an empty folder to a conversation in which you review and allow an agent action.
It uses a local launch, so Heartwood keeps its state in `.heartwood/` inside the folder.
Use synthetic or non-sensitive files.

## Prerequisites

You need:

- a [Heartwood installation](install.md);
- a terminal, the application for entering text commands;
- a new folder that Heartwood may read and modify; and
- access to one model connection, or enough disk and memory for a model that Heartwood runs in the current environment.

On Terra or Stanford Carina, follow the [platform guide](../platforms/index.md) first; installation and storage differ there.
If an operator runs Heartwood for you in a managed deployment, use the launch link and setup the operator provides; the installation and setup steps below apply to a local launch.

## Create a Project Folder

```bash
mkdir heartwood-first-project
cd heartwood-first-project
```

`mkdir` creates the folder and `cd` enters it.
The current directory is the Heartwood project.

!!! warning "Choose the boundary carefully"
    Heartwood works with files in this folder and its subfolders.
    Do not start it from your home directory, a shared storage root, or a folder that contains unrelated files.

## Check the Environment

```bash
heartwood doctor
```

The command reports the project path, the detected platform and the next setup step without creating `.heartwood/` or changing project files.
On a new project it reports `Readiness: setup-required`.

## Start Heartwood

```bash
heartwood
```

Heartwood shows the resolved project path before it creates any state.
In an empty folder, choose **Add the synthetic first example** to add two small synthetic CSV files under `data/`, or **Use this project** to keep the folder unchanged.

### Choose a Model Connection

Heartwood lists the model connections that the detected platform permits.
Choose one, select a model returned by that service, and enter a credential only when the route requires one.
**Sign in with ChatGPT** uses a Plus or Pro account and a one-time browser code; **OpenAI API**, **Anthropic** and **Stanford AI API Gateway** use API keys.

For **Run with Heartwood**, Heartwood shows its recommendations and an **Other Hugging Face model** option, with the model format, license, download size, runtime and resource guidance, before anything is downloaded.
See [Models](../models/index.md) for the differences between routes.

## Ask for a Bounded Task

If you added the synthetic example, enter:

```text
Inspect the CSV files under data. Summarize their columns and row counts in a new file named summary.md. Do not access files outside this project.
```

Heartwood shows activity while the model works.
When the agent proposes one or more related actions, Heartwood presents them as one action set, with the tool, risk and relevant arguments of each member.

## Review the Action Set

A local launch starts in **Review Every Action**, so every action set waits for you.
Inspect each proposed command and file operation, then choose **Reject** or **Allow Once** for the complete set.
In the plain terminal (`heartwood --plain`), enter `/allow` or `/reject`.

## Verify and Resume

After allowing the expected action, inspect the result:

```bash
ls
cat summary.md
```

Exit Heartwood with `/exit`, then run `heartwood` again from the same folder.
The project configuration and session history are still in `.heartwood/`.
Use `/replay` to review the durable session events and `/audit-export` to write a scrubbed JSON Lines export.

## Next Steps

- Read [Projects and Private State](project.md) before adding real project files.
- Learn the [core workflow](../use/index.md) and the [terminal](../use/terminal.md), [browser](../use/browser.md) and [notebook](../use/notebooks.md) interfaces.
- Read [Actions and Audit History](../use/actions-audit.md) for review modes and what each one allows.
