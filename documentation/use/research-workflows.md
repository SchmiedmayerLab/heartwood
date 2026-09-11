<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Run a Research Workflow

A research workflow organizes an analysis into stages with named inputs, output files, checks, and review points.
The agent uses its normal coding tools; you review proposed actions through the same conversation interface.
Start in a new session with a configured model and project-local input files.

## Choose a Workflow

| Workflow | Purpose | Inputs |
|---|---|---|
| Dataset Readiness Review | Inspect data quality and review a report before modeling | CSV dataset and JSON data dictionary |
| Reproducible Baseline Analysis | Plan a baseline, run it, reproduce its outputs, and review the report | CSV dataset, JSON data dictionary, and research question |

!!! note "Scope of the maintained checks"
    The baseline checks cover a single-predictor linear model with a numeric outcome, held-out metrics, a mean-only comparator, and group-omission sensitivity checks.
    The data dictionary must identify separate training and test groups.
    Other analyses can use an ordinary conversation, but do not receive these workflow completion checks.
    An unavailable workflow cannot be selected in the interface.
    Each inspected text file must fit within 512 KiB and 10,000 lines with the default inspection settings.
    Use a deliberately prepared, representative extract for these bounded checks; a passing extract does not validate the full dataset.

## Prepare the Inputs

Keep original data unchanged and choose an unused output folder, such as `results`.
Paths are relative to your [project folder](../start/project.md), not your computer's root directory.
The JSON dictionary names the data columns and their intended roles:

```json
{
  "grouping_key": "participant_id",
  "primary_key": ["participant_id"],
  "outcome": "score",
  "permitted_predictors": ["baseline_score"],
  "excluded_predictors": {"future_score": "Measured after the outcome"},
  "split": {"column": "partition", "train": "train", "test": "test"},
  "validity": {"baseline_score": {"minimum": 0, "maximum": 100}},
  "notes": "Synthetic example; replace column names and constraints with your study definitions."
}
```

Every named column must exist in the CSV, except excluded predictors that are not present.
For repeated observations, include all identifying columns in `primary_key` and keep each participant entirely within one partition.
Choose the partition and analysis question before asking the agent to fit a model.
The dictionary describes your intended analysis; it does not establish that the study design is valid.

## Start and Review

=== "Browser"

    1. Open **Research** in a new session.
    2. Choose the workflow, enter input paths and your question, and review **Work Limits**.
    3. Select **Start Workflow** to save the setup, then **Run** for the first stage.
    4. Open **Conversation** to inspect and allow or reject proposed tool actions.
    5. When the agent finishes, select **Check Results** in Research.
    6. Inspect the listed artifacts and checks before accepting a stage that requires review.
    7. Run the next available stage until the workflow is complete.

=== "Terminal"

    Run `heartwood`, then enter `/workflow` to open the keyboard-accessible workflow form.
    Select a workflow and provide its inputs.
    Reopen `/workflow` to inspect progress and select the next available stage action.
    Tool approvals stay in the normal conversation review flow.

    In plain mode, use `/workflows` to list definitions and `/workflow` to inspect the current stage:

    ```text
    /workflow start baseline-analysis data=data.csv dictionary=dictionary.json question="Does baseline score predict the held-out outcome?" output=results
    /workflow
    /workflow run
    ```

    After the agent finishes, inspect `/workflow` again and use the displayed command, such as `/workflow evaluate` or `/workflow accept`.
    Decisions apply to the state you inspected; stale decisions are rejected.

=== "Notebook"

    The [notebook bridge](notebooks.md#research-workflows) exposes the same definitions, run state, and available requests.
    It does not run a second agent or maintain separate workflow state.

## Understand the Checks

**Checking results** reads the bound inputs and expected artifacts and compares them using maintained checks.
For reproduction, the workflow also requires a separately approved execution of the unchanged analysis in a fresh process and output directory.
An agent's claim that it reran the analysis is not sufficient.

**Accepting a stage** records your review of its current evidence.
It does not grant approval for later tool actions or certify a scientific conclusion.
If inputs or reviewed artifacts change, the gateway refuses to continue using stale evidence.

**Experiment records** connect each accepted stage's outputs to its declared inputs, code, and recorded actions.
Open **Experiment Records** in the browser's Research tab or enter `/experiments` in the terminal to inspect them.
The notebook exposes the same records through `view.experiments` and its Experiment Records widget section.
See [Experiment Records](../architecture/experiments.md) for evidence boundaries and project-wide export.

**Work limits** bound work at observed execution boundaries.
They are not a guaranteed provider billing cap: an in-flight request can finish before its usage is reported, and some providers do not report every usage field.

## Recover or Stop

| Situation | Next Step |
|---|---|
| A check fails | Inspect the failed check and artifacts; do not accept the stage as complete |
| The agent still has pending actions | Review the complete action set in Conversation before changing stages |
| Evidence changed after review | Refresh the workflow and check results again before deciding |
| Inputs changed or the workflow cannot proceed | Cancel the run and start a new session with corrected inputs |
| You need to stop active work | Pause the agent first; after it settles and pending actions are resolved, cancel the workflow |

Cancellation retains files and history; it does not undo tool actions.
Use [Files and Changes](browser.md) to inspect outputs and [audit export](actions-audit.md) to retain the session's scrubbed action history.
