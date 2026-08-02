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

On Terra, select the **Python 3 (Heartwood)** kernel.
The default Terra kernel and the `python` command in a terminal may not contain Heartwood; use the named kernel instead of installing another copy.

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
    "Inspect the analysis code and explain the existing data-quality checks. Do not modify files."
)
view.lifecycle.status
```

Use the researcher-facing status when presenting the state in notebook output:

```python
view.researcher_status.label
view.researcher_status.detail
```

`chat()` returns immediately after the background OpenHands run starts.
Poll the shared projection when a notebook cell should wait for the next interactive boundary:

```python
import time

while view.lifecycle.status == "running":
    time.sleep(0.5)
    view = session.replay()

[(message.label, message.content) for message in view.conversation]
```

You can submit guidance with another `session.chat(...)` call while the task is running.
Use `session.pause()` and `session.resume()` to control the background run.

## Review an Action Set

A pending action set appears in `view.pending_approval`.
Inspect every member before resolving the group:

```python
pending = view.pending_approval
assert pending is not None
[(action.tool_name, action.state, action.details, action.arguments) for action in pending.actions]
```

Allow or reject the complete set with its group identifier:

```python
view = session.approve(group_id=pending.group_id)
# Or: view = session.deny(group_id=pending.group_id)
```

The decision applies to every action displayed in that OpenHands action set.

All correlated action records remain available through `view.actions`.
Each record includes its OpenHands identifiers, grouped decision, typed details, state, bounded outcome, and explicit affected-path evidence.

Task progress is available through `view.task_plan`.
Combined model usage is available through `view.usage`, and agent and condenser usage are separated in `view.usage_by_purpose`.
Sequential specialist work and parent lineage are available through `view.subagents`.
The small gateway-owned set in `view.suggestions` provides the same editable next-step prompts shown in the terminal and browser.

## Inspect Files and Changes

The notebook bridge exposes the same bounded read-only service as the terminal and browser without building a separate notebook file browser:

```python
tree = session.files()
source = session.file("analysis/cohort.py")
changes = session.changes()
diff = session.diff("analysis/cohort.py")
```

The returned typed mappings label binary, truncated, unavailable, non-Git, and unsupported states.
They exclude `.heartwood/` and `.git/`, reject unsafe paths, and apply the same count, depth, line, and byte limits as the other interfaces.

## Export the Audit Record

```python
export = session.audit_export()
export["filename"]
export["content"]
```

The returned JSON Lines content is the same scrubbed export downloaded by the browser.

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
