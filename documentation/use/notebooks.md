<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood From a Notebook

The notebook bridge gives Python access to the same project, model settings, sessions, action review, and audit history as the terminal and browser.
Configure the model with `heartwood` or the browser first; the notebook is for working beside exploratory code, not for first-time setup.

## Submit a First Task

Open a notebook whose working directory is the project.
On Terra, select the **Python 3 (Heartwood)** kernel; the default kernel and the `python` command in a terminal may not contain Heartwood.

```python
from heartwood.notebook import NotebookSession

session = NotebookSession(session_id="notebook-analysis")
session.startup_plan()
```

`NotebookSession()` uses the notebook process's current directory as the project; it does not take a separate workspace path.
`session.project_readiness()` and `session.platform_capabilities()` show whether a model is configured and what this platform supports.

```python
view = session.chat(
    "Inspect the analysis code and explain the existing data-quality checks. Do not modify files."
)
view.researcher_status.label
```

`chat()` returns as soon as the background run starts.
Poll the session until it reaches the next point that needs you:

```python
import time

while view.lifecycle.status == "running":
    time.sleep(0.5)
    view = session.replay()

[(message.label, message.content) for message in view.conversation]
```

Call `session.chat(...)` again while the task runs to add guidance, and `session.pause()` or `session.resume()` to control the run.

## Review an Action Set

A pending action set appears in `view.pending_approval`.
Inspect every member, then allow or reject the complete set by its group identifier:

```python
pending = view.pending_approval
assert pending is not None
[(action.tool_name, action.details, action.arguments) for action in pending.actions]

view = session.approve(group_id=pending.group_id)
# Or: view = session.deny(group_id=pending.group_id)
```

The decision applies to every action in that set.
`view.actions` holds every action record with its state, bounded outcome, and affected paths.
An action Heartwood allowed under a review rule has that rule in `automatic_rule` and never appears as your approval.

The notebook runs without a deployment manifest and without an isolated worker.
**Review Every Action** is the default, and a mode you select lasts until this notebook's gateway closes:

```python
session.action_settings()
session.select_action_review_mode("review-changes")
```

See [Review Modes](actions-audit.md#review-modes) for what each mode allows and when it is available.

## Read the Session

| Attribute | Contents |
|---|---|
| `view.researcher_status` | Label and detail of the current state |
| `view.task_plan` | Task Tracker progress |
| `view.usage`, `view.usage_by_purpose` | Model usage in total, and separately for the agent and condenser |
| `view.subagents` | Specialist work and its parent lineage |
| `view.suggestions` | Up to two editable next-step requests, the same as in the terminal and browser |
| `view.experiments` | Experiment records of the active session's workflow |

`session.specialist_settings()` returns the same [specialist](specialists.md) catalog as the other interfaces.

## Research Workflows

Inspect the available workflows without starting model work:

```python
catalog = session.research_workflows()
[(entry.definition.label, entry.available) for entry in catalog.workflows]
```

Availability means the workflow's checks are implemented, not that the selected model is qualified for your question.

Start a [research workflow](research-workflows.md) in a new session:

```python
from heartwood.schemas.workflows import WorkflowStart

view = session.workflow(
    WorkflowStart(
        action="start",
        workflow_id="baseline-analysis",
        inputs={
            "data": "data.csv",
            "dictionary": "dictionary.json",
            "question": "Does baseline score predict the held-out outcome?",
        },
        output_directory="results",
    )
)
[(control.control_id, control.label) for control in view.workflow_controls]
```

Setup does not call the model.
Submit the stage request you want from the controls you inspected:

```python
run_stage = next(control for control in view.workflow_controls if control.control_id == "run")
view = session.workflow(run_stage.request)
```

Poll and review actions as above.
After the stage settles, the controls offer result checking and any required stage review.
The gateway rejects a request built from an older revision of the view.

## Experiment Records, Files, and Audit Export

```python
records = session.experiment_records()
exported = session.export_experiments()
print(exported.sha256)
```

`exported.jsonl` holds the same verified JSON Lines as the browser download and `heartwood experiments export`.
It can contain sensitive paths and metadata; handle it as project data, not as an anonymized or signed record.
See [Experiment Records](../architecture/experiments.md).

The project views are read-only and use the same limits as the terminal and browser:

```python
tree = session.files()
source = session.file("analysis/cohort.py")
changes = session.changes()
diff = session.diff("analysis/cohort.py")
```

They exclude `.heartwood/` and `.git/`, reject unsafe paths, and label binary, truncated, unavailable, non-Git, and unsupported states.

`session.audit_export()` returns the scrubbed [audit export](actions-audit.md#audit-export) with its `filename` and `content`.

## Keep Required Services Running

The notebook bridge does not supervise a downloaded model runtime or keep a token entered in another process.
Start Heartwood normally so it can prepare compute and inference, keep that process running, and use a different session identifier in the notebook.

On a workstation, `session.browser_url()` returns the browser address; on Terra and Stanford Carina it returns `None`.
Do not construct a proxy address manually.

## Release the Session

Only one process writes a session at a time.
Close the notebook gateway before continuing the same session elsewhere:

```python
session.close()
```

`with NotebookSession(...) as session:` releases resources and process-scoped credentials automatically.
