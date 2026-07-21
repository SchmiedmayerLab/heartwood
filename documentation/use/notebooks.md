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

## Keep Required Services Running

The notebook bridge does not independently supervise a downloaded model runtime or retain a token entered in another process.
Start Heartwood normally so the shared startup planner can prepare required compute and inference, then keep that process running while the notebook uses a different session identifier.

On a workstation, `session.browser_url()` returns the direct browser URL when the platform supports it.
Terra and Stanford Carina do not expose a supported Heartwood browser route, so this method returns `None` there.
Do not construct a proxy URL manually.

## Release the Session

Close the notebook gateway before continuing the same session in another process:

```python
session.close()
```

For a bounded workflow, use `with NotebookSession(...) as session:` so resources and process-scoped credentials are released automatically.
