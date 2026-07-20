<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Your First Heartwood Project

This guide takes you from an empty folder to a conversation in which you can review an agent action.
Use synthetic or non-sensitive files for this first run.

## Before You Begin

You need:

- a [supported Heartwood installation](install.md);
- a terminal, which is an application for entering text commands;
- a dedicated folder that Heartwood may read and modify; and
- access to one model connection, or enough disk and memory for a model that Heartwood runs in the current environment.

If you work in Terra or Stanford Carina, follow the [platform guide](../platforms/index.md) first because installation and access differ.

## Create a Project Folder

Open a terminal and run:

```bash
mkdir heartwood-first-project
cd heartwood-first-project
```

`mkdir` creates the folder.
`cd` enters it, making it the current directory and therefore the Heartwood project.

!!! warning "Choose the boundary carefully"
    Heartwood may work with files in this folder and its subfolders.
    Do not start Heartwood from your home directory, a shared storage root, or another folder that contains unrelated files.

## Check the Environment

```bash
heartwood doctor
```

The command reports the project path, detected platform, and the next setup step without creating `.heartwood/` or changing project files.
On a new project, **setup-required** is expected.

## Start Heartwood

```bash
heartwood
```

Heartwood shows the resolved project path before it creates private project state.
In an empty folder, choose **Add the synthetic first example** to add two small synthetic CSV files under `data/`, or choose **Use this project** to keep the folder unchanged.

Heartwood then lists only the model connections available in the detected environment.

### Choose a Model Connection

Choose a connection, select a model returned by that service, and provide a credential only when the selected route requires one.

For a Heartwood-managed model, Heartwood shows a small set of recommendations and an **Other Hugging Face model** option.
It displays the model format, license metadata, download size, runtime, and resource guidance before downloading.

## Ask for a Bounded Task

If you added the synthetic example, enter:

```text
Inspect the CSV files under data. Summarize their columns and row counts in a new file named summary.md. Do not access files outside this project.
```

Heartwood displays activity while the model works.
When the agent proposes one or more related actions, Heartwood presents them as one set with the tool, risk, and relevant arguments for each member.

## Review the Action Set

Inspect every proposed action.
Use the arrow keys to select **Allow all once** or **Reject all**, then press Enter.
In the plain terminal, enter `/allow` or `/reject`.

## Verify and Resume

After allowing the expected action, inspect the project:

```bash
ls
cat summary.md
```

Exit Heartwood with `/exit`, then run `heartwood` again from the same folder.
The same project configuration and session history remain under `.heartwood/`.

Use `/replay` to review the durable session events and `/audit-export` to create a scrubbed JSON Lines export.

## Next Steps

- Learn the [core workflow](../use/index.md).
- Choose between the [terminal](../use/terminal.md), [browser](../use/browser.md), and [notebook](../use/notebooks.md) interfaces.
- Compare [model connections](../models/index.md).
- Read [Projects and Private State](project.md) before adding real project files.
