<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood CLI

The `heartwood` command opens an interactive coding-agent session for research work. You can describe a task in natural language, follow the agent's responses and proposed actions, allow or reject individual actions, pause or resume work, and return to the persisted conversation later.

Start the full-screen terminal interface:

```bash
heartwood
```

On first use, bare `heartwood` opens the setup flow instead. `heartwood doctor` reports environment, storage, accelerator, model-route, credential-reference, and policy readiness without changing state. `heartwood setup` configures a validated model route and **Ask Every Time** action confirmation, then later bare invocations open the conversation.

Enter a request at the prompt. During the conversation, use `/help` to list available commands. Common commands include:

```text
/allow <id>   Allow a proposed action once
/reject <id>  Reject a proposed action
/pause        Pause the session
/resume       Resume the session
/status       Show the active model and policy status
/replay       Replay the persisted conversation
/audit-export Export the scrubbed audit record
/exit         Close the terminal interface
```

Heartwood automatically uses the line-oriented interface when a full-screen terminal is unavailable. You can also select it explicitly for SSH sessions and basic terminals:

```bash
heartwood chat --plain
```

For scripts or a single task, submit a prompt without opening an interactive session:

```bash
heartwood chat --prompt "Inspect the workspace and summarize the analysis."
```

Use `--session-id` to return to a named conversation, and `--workspace` to choose where Heartwood stores local session state:

```bash
heartwood --session-id cohort-review chat
heartwood --workspace /path/to/state --session-id cohort-review chat
```

Before starting a model-backed conversation, inspect the available connections and select a model exposed by a local runtime, research environment, or configured provider:

```bash
heartwood models list
heartwood models refresh local
heartwood models connect local <model-id>
```

Additional commands manage action-confirmation settings, Skills, session replay, audit export, environment detection, and the web interface. Run `heartwood --help` or `heartwood <command> --help` for the complete command reference.
