<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood CLI

The `heartwood` command opens an interactive coding-agent session for the current directory. The agent works on that directory and its descendants; Heartwood keeps configuration, conversations, downloaded models, Skills, logs, and audit records in a private `.heartwood/` directory beside the project files.

Start the full-screen terminal interface:

```bash
heartwood
```

On first use, bare `heartwood` guides model selection and then opens the conversation. Later invocations resume the same project setup. A provider token entered at the prompt is held only for that process; platform-managed credential bindings can survive a restart without storing a secret in `.heartwood/`.

Enter a request at the prompt. During the conversation, use `/help` to list available commands. Common commands include:

Heartwood shows an animated activity message while a request is running. If a response takes longer than expected, it reports elapsed time without guessing which internal agent step is active. Model downloads and Heartwood-managed server startup use their own progress and readiness messages because those operations can take several minutes.

```text
/allow        Allow the complete pending OpenHands action set once
/reject       Reject the complete pending OpenHands action set
/pause        Pause the session
/resume       Resume the session
/status       Show the active model and policy status
/replay       Replay the persisted conversation
/audit-export Export the scrubbed audit record
/exit         Close the terminal interface
```

Heartwood automatically uses the line-oriented interface when a full-screen terminal is unavailable. You can also select it explicitly for SSH sessions and basic terminals:

```bash
heartwood --plain
```

Run `heartwood` from the directory the agent should edit. Use `--session-id` to return to a named conversation:

```bash
cd /path/to/analysis
heartwood --session-id cohort-review
```

For scripts or a single task, submit a prompt without opening an interactive session:

```bash
heartwood --prompt "Inspect this project and summarize the analysis."
```

Before starting a model-backed conversation, inspect the available connections and select a model exposed by Heartwood, a research environment, or a configured provider:

```bash
heartwood models list
heartwood models refresh heartwood
heartwood models connect heartwood <model-id>
```

`heartwood doctor` checks project, platform, model, credential-binding, and compute readiness without changing state. Additional commands manage model downloads, action confirmation, Skills, replay, audit export, platform detection, Heartwood-managed runtime launch, and the web interface. Run `heartwood --help` or `heartwood <command> --help` for the complete command reference.
