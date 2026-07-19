<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Terminal, Browser, and Notebook

All three interfaces use the project in the current directory and the same session gateway. They differ in presentation and in which setup tasks they own.

## Compare the Interfaces

| Capability | Terminal | Browser | Notebook |
|---|:---:|:---:|:---:|
| Guided model setup | Yes | Yes | Inspect configured state |
| Start a downloaded model | Yes | Yes, through `launch --web` | No |
| Conversation and grouped action decisions | Yes | Yes | Yes |
| Replay and audit export | Yes | Yes | Yes |
| Skill inspection and management | Yes | Yes | No |
% TODO: What does that even mean?!
| Reliable fallback over a remote shell | Yes | No | No |

Use one active writer for each session. Wait for a turn to become idle before continuing it elsewhere.

## Terminal

Start the interactive terminal:

```bash
cd /path/to/project
heartwood
```

% TODO: It's quite confusing that we have different commands there ... we should bundle this and rather use input arguments if we want to skip elements in the customizable setup ...
Use `heartwood launch` when Heartwood must start a downloaded local model. Use `heartwood chat --plain` for a basic terminal.

The terminal is the primary interface on Stanford Carina and the reliable fallback when a managed platform cannot expose the browser route.

## Browser

The browser application is included in the generic containers, Terra images, and source checkouts after the web build. It is not included in the published generic native archive.

Start the browser service from the project:

```bash
heartwood serve
```

Open `http://127.0.0.1:8767/` on the same machine. For a downloaded model, use:

```bash
heartwood launch --web
```

The browser provides model setup, conversations, grouped action review, Skills, readiness details, and audit activity. Enter a hosted-provider token in browser setup so it remains available to the service process handling that conversation.

![Heartwood browser conversation with a synthetic analysis](assets/web-reference-analysis.png)

Open **Settings** to inspect the active model, local-model choices, resource guidance, and action-confirmation mode.

% TODO: We should provide more dtailed elmetns here ...
% TODO: We don't really have a good equivalient for the CLI or even the more interactive CLI: we should have more extensive documentation for that as well ...

### Browser Access on Managed Platforms

% TODO: is that too technical? And shouldn't this then be part of the relevant platofrm defintions
Do not expose port `8767` publicly. Terra uses an authenticated Jupyter proxy and requires the complete runtime-specific URL. The [Terra guide](terra-jupyter-demo.md) explains how the terminal or tutorial notebook provides that link.

Stanford Carina does not currently have a documented authenticated Heartwood browser route. Use the terminal there.

% TODO: Why is the notebook mentioned here in the browser setup?! This doesn't make any sense ...
## Notebook

Start the notebook kernel from the configured project directory:

```python
from heartwood.notebook import NotebookSession

session = NotebookSession(session_id="analysis")
view = session.replay()
print(view.event_count)
```

`NotebookSession` can inspect project readiness and models, submit a task, approve or reject the pending action group, pause or resume, replay the session, and export the audit record.

The notebook does not start a downloaded model or retain a token entered in another process. Keep `heartwood launch --web` running in a terminal when the notebook uses a Heartwood-managed local model. For a hosted connection, the deployment must make the selected profile's credential available to the notebook kernel. On Terra, the [tutorial notebook](terra-jupyter-demo.ipynb) provides an end-to-end synthetic example.

% TODO: Shoulnn't this be mentioed here but generally be part of a larger documentation element that is byeond the browser setup.
## Shared State

Model and action settings are saved with the project. Browser storage is not the source of truth. A new interface process reconstructs the same configuration and events from the project's `.heartwood/` directory.

Use the same session identifier to replay a browser-created session from the terminal:

```bash
heartwood --session-id <session-id> replay
```
