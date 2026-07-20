<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood From a Notebook

The notebook bridge provides a Python view of the same project, model settings, sessions, action controls, and audit history used by the terminal and browser.
Use it when agent interaction belongs beside exploratory code and results rather than as a replacement for the terminal setup flow.

## Start in the Project

Open a notebook whose working directory is the intended project, then verify it:

```python
from pathlib import Path

project_root = Path.cwd().resolve()
project_root
```

Create a session:

```python
from heartwood.notebook import NotebookSession

session = NotebookSession(session_id="notebook-analysis")
session.startup_plan()
```

`NotebookSession()` uses the notebook process current directory.
Do not pass a separate workspace path to represent the same project.

## Confirm Readiness

```python
session.project_readiness()
session.platform_capabilities()
```

Configure the model through `heartwood` or the browser first when possible.
The notebook API also exposes the shared connection catalog and model-selection methods for programmatic workflows.

## Submit a Task

```python
view = session.chat(
    "Inspect the analysis code and explain the existing data-quality checks. "
    "Do not modify files."
)
view.chat
```

Pending actions appear in `view.approval_controls`.
Inspect every `actions` member before resolving the group:

```python
pending = view.approval_controls[0]
[(action.tool_name, action.summary, action.arguments) for action in pending.actions]
```

Allow or reject the complete set with one member identifier:

```python
view = session.approve(tool_call_id=pending.target_id)
# Or: view = session.deny(tool_call_id=pending.target_id)
```

The identifier selects the pending OpenHands callback; the decision applies to the complete displayed action set.

## Export the Audit Record

```python
view = session.audit_export()
view.export_actions
```

## Open the Browser Beside Jupyter

On a supported Jupyter platform, start `heartwood --interface web` in a terminal and keep it running.
`session.web_proxy_url()` returns the authenticated proxy path only when the environment provides enough routing information; render that path as a link in the current notebook so the browser retains the Terra host and authentication context.

```python
from IPython.display import Markdown
from IPython.display import display as display_markdown

browser_path = session.web_proxy_url()
if browser_path is None:
    display_markdown(Markdown("Heartwood could not verify a browser proxy route in this kernel."))
else:
    display_markdown(Markdown(f"[Open the Heartwood browser]({browser_path})"))
```

Do not replace a missing route with `/proxy/8767/`.
Use the terminal interface when the current Jupyter environment does not expose enough authenticated routing evidence.

The notebook bridge does not independently supervise a downloaded model runtime.
Start Heartwood normally so the shared startup planner can prepare required compute and inference.

## Release the Session

Close the notebook gateway before continuing the same session in another process:

```python
session.close()
```

For a bounded workflow, use `with NotebookSession(...) as session:` so resources and process-scoped credentials are released automatically.
