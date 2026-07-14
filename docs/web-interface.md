<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Use Heartwood in a Browser

The browser interface provides the same conversations, model selection, action review, Skills, and audit history as the `heartwood` terminal command. Start it from the directory you want Heartwood to treat as the project. Configuration and progress remain with that project in `.heartwood/`.

## Open the Interface

From a source checkout with the web assets built:

```bash
cd /path/to/analysis
uv run --project /path/to/heartwood heartwood serve
```

Open `http://127.0.0.1:8767/`. The [container guide](container-images.md) provides the shortest setup when Heartwood is not installed locally. On Terra, use the authenticated Jupyter proxy route described in [Heartwood on Terra](terra-jupyter-demo.md) instead of exposing the port publicly.

When Heartwood is managing a downloaded local model, start both services together:

```bash
heartwood launch --web
```

## Choose a Model

Open **Settings**, choose where the model runs, and select one of the models reported by that service:

- **Local** shows a running local service and reviewed models downloaded for this project.
- **Research environment** shows connections supplied by the platform, including every model available to the current identity.
- **OpenAI** and **Anthropic** ask for a token for the current server process and request the model list directly from the provider.
- **Custom API** connects to another service that implements the OpenAI API format.

Heartwood stores the selected model and a non-secret credential binding. A token entered in the browser remains only in the running gateway process. The project policy must authorize both model discovery and model use before a connection succeeds. See [Choose a Model](model-connections.md) for deployment and credential details.

## Work with the Agent

Create or select a conversation, then describe the result you need. Heartwood displays messages, proposed commands and file edits, tool results, and completion status in one timeline.

![Heartwood synthetic reference analysis showing the cohort, baseline, and aggregate-export conversation](assets/web-reference-analysis.png)

When an action needs confirmation, review every member of the displayed set. **Allow all once** continues the complete OpenHands action set; **Reject all** executes none of it. **Ask Every Time** is the default. A deployment may permit **Auto-Approve Low Risk**, but medium-, high-, and unknown-risk sets still require review.

The synthetic reference workflow in [Heartwood on Terra](terra-jupyter-demo.md#run-the-synthetic-workflow) demonstrates cohort creation, aggregate checks, approval, replay, and audit export without controlled data.

## Resume from Another Interface

The session identifier is shared by the browser, terminal, and notebook bridge. From the same project directory:

```bash
heartwood --session-id <session-id> replay
heartwood --session-id <session-id> chat
```

Use one active writer for a session. Wait for the current agent turn to become idle before opening that session from another interface.

In a notebook process whose current directory is the project:

```python
from heartwood.notebook import NotebookSession

session = NotebookSession(session_id="<session-id>")
print(session.replay().event_count)
```

## Inspect and Export Activity

Open **Activity & audit** to inspect ordered route decisions, action proposals, human decisions, tool outcomes, and errors. **Export audit** creates a content-minimized JSON Lines record. It omits prompts, model responses, action summaries, paths, row values, and secrets by default.

The notebook-width layout keeps the conversation and controls usable behind Jupyter's proxy:

![Heartwood synthetic reference analysis at a narrow notebook viewport](assets/web-notebook-viewport.png)

Both documentation screenshots contain synthetic data only.
