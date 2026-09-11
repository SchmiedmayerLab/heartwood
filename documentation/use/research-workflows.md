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
| Independent Result Verification | Rebuild an analysis environment, re-run the original code, and compare outputs | Dataset, Python script, original results, environment record, and dependency lock |

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

Readiness counts describe the original input without dropping rows: row and arm counts include duplicates and repeated observations, while the subject count uses distinct grouping-key values.
The report explains leakage concerns separately from the exact column names in the structured result.

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

### Work Limits

Review the displayed limits before starting a stage or requesting specialist review.
Each stage allows up to 15 minutes, 40 model calls, 600,000 cumulative tokens, 45 proposed actions, and $1 of reported model cost.
The entire workflow also has limits: 30 minutes, 80 calls, 1.6 million cumulative tokens, 100 actions, and $4 of reported model cost.
The first reached limit prevents admitting more work.

Cumulative tokens include input context sent again on later calls; they are not the model's context-window size.
Elapsed time includes time waiting for action review.
Specialist review and corrections share the stage's remaining allowance.
An in-flight request can exceed a limit before Heartwood receives its usage, and some providers do not report cost; these are execution safeguards, not guaranteed billing caps.
When a limit is reached, retain the outputs and inspect the session before starting another bounded task.

## Verify an Existing Analysis

Independent Result Verification preserves the original analysis and writes fresh reproduction outputs.
The script must accept `--data FILE --output-dir NEW_FOLDER` and create `metrics.json` and `predictions.csv` in that folder.
Heartwood compares the generated files byte for byte; matching outputs do not establish scientific validity.

Retain the original analysis's environment record and dependency lock alongside its code and results.
For an existing analysis environment, select its Python executable explicitly:

```sh
heartwood experiments environment --python analysis-env/bin/python > python-environment.json
```

Replace `analysis-env/bin/python` with the executable used for the original analysis.
For a notebook, `session.verification_environment(python="analysis-env/bin/python")` returns the same typed record.
Omitting `--python`, or choosing **Export Server Python Environment** in the browser, captures Heartwood's server environment instead; use it only when that was the analysis environment.
Capturing today's environment does not recover an unknown historical environment.

??? details "Prepare the dependency lock"

    Use a standard `pylock.toml` file containing the analysis's exact dependencies and wheel hashes.
    [uv's locking tools](https://docs.astral.sh/uv/pip/compile/) can generate it from pinned analysis requirements:

    ```sh
    uv pip compile requirements.txt --python analysis-env/bin/python --format pylock.toml -o pylock.toml
    ```

    Review the lock against the original environment record; newly resolving unpinned requirements does not reproduce a historical environment.
    Heartwood accepts hash-pinned wheels from HTTPS URLs without credentials or project-relative wheel paths.
    Source builds, editable projects, and authenticated package indexes are not supported by reconstruction.
    For offline use, retain the required wheels in the project and reference them using `path` entries in the lock.
    An analysis using only the Python standard library can use a lock with `lock-version = "1.0"`, `created-by = "researcher"`, and `packages = []`.

In **Independent Result Verification**, select the record as **Environment Record** and the lock as **Dependency Lock**.
The first stage proposes one setup action; approving it permits downloading and installing the locked packages into a fresh environment under `.heartwood/runtime/`.
Heartwood and vLLM dependencies are not changed.
Only approve dependencies from sources you trust: package hashes establish identity, not safety.

Review the observed environment before continuing.
Reproduction uses its isolated Python, excluding inherited environment variables, user-site packages, and implicit project-module imports.
Use installed dependencies and a self-contained entry-point script.
Heartwood rechecks the observed versions immediately before execution and stops if they changed.

If setup fails, retain the failure evidence and start a new verification with a fresh output folder after resolving the cause.
Missing Python versions must be installed deliberately; reconstruction never downloads an interpreter.
The operating-system family and architecture must match the record.
For slow or unavailable downloads, stage wheels in the project first.
An interrupted setup is not reusable, and a still-running terminal command is not successful execution evidence.
Stop any outstanding command before retrying; Heartwood never overwrites a previous environment or result folder.

The record includes Python implementation and version, operating-system family, architecture, and installed distribution names and versions.
It does not attest binaries, native libraries, GPU drivers, or package contents.
Additional packages beyond a declared requirement set are not verified.
See [Experiment Records](../architecture/experiments.md) for provenance and evidence boundaries.

## Understand the Checks

### Request an Advisory Review

After a stage with declared specialists finishes, choose **Review Analysis** before moving to the next stage.
Heartwood records the declared evidence and asks the selected specialists to review it through the normal agent and tool-approval flow.
This uses the stage's remaining work budget; it does not grant permission to change files.

Heartwood checks the findings automatically when the review finishes.
In plain terminal mode, inspect `/workflow` and use `/workflow request-review` when offered.
The notebook receives the same controls and review state.

The assessment distinguishes verified observations, rejected claims, unsupported conditions, stale evidence, and unavailable evidence.
Checks cover analysis plans that conflict with the baseline's predictor, outcome, grouping, or split requirements; invalid Python syntax; supported baseline result inconsistencies; and artifact byte mismatches.
Plan findings require readable, valid input data and a data dictionary; they do not establish that the research question or statistical method is scientifically appropriate.
A byte mismatch is not proof of re-execution, and a review with no findings is not proof of correctness.
Missing specialist results are reported as unavailable rather than a successful review.

Each stage accepts one advisory review.
Changing its conversation context invalidates the assessment; Heartwood reports that review as unavailable rather than treating redirected work as the original review.
Reviewing does not automatically repair files or accept the stage.
Continue to use **Check Results** and the normal researcher review after inspecting the findings.

### Parallel Reviews

Reviews are sequential by default.
If your deployment has configured qualified parallel reviews, a stage with multiple reviewers offers **Preview Parallel Review**.
The preview shows the worker count and work limits without starting the model.
Choose the displayed parallel-review action to authorize that exact plan, or choose **Review Analysis** to keep the review sequential.
The terminal, browser, and notebook use the same plan and controls.

Plan authorization does not approve tool actions.
Review the complete proposed specialist action set in the conversation before allowing it.
The status distinguishes a preview, an authorized plan, and admitted work; admission alone does not establish that every reviewer finished or that the findings are correct.
Work limits apply to the stage's remaining budget and are not guaranteed provider billing caps.

If the files, model route, workflow, or qualification evidence change, refresh the preview before deciding.
Expired or changed authorization cannot start parallel work.
After interruption, Heartwood retains the review record and does not automatically repeat an admitted batch.

A benchmark environment may instead display **Experimental parallel review** and **Run Trial with ... Parallel Specialists**.
This is a bounded test, not a recommendation that the model is faster or more reliable.
The same explicit plan consent and action review still apply.

### Correct Verified Findings

When a review verifies a supported defect, select **Correct Findings (Up to 2 Attempts)**.
In the terminal, use the displayed `/workflow correct` command or the workflow form.
This authorizes a bounded correction task, not its tool actions: review each proposed action set in the conversation as usual.

Heartwood gives the agent fresh output paths and rechecks the result independently after each attempt.
If the defect remains, it can make a second attempt within the same stage's remaining work limits.
Original evidence, accepted earlier stages, and previous attempts remain preserved.
The browser, terminal, and notebook show the attempts and their checks from the same session state.

For a reproduction discrepancy, the agent receives the unchanged program's rerun command and a fresh output location.
It may first propose creating the parent folder, then request approval for the rerun separately.
Matching copied files do not satisfy the reproduction stage: Heartwood must have recorded the approved execution and unchanged inputs.

A successful correction means the specific checked defect is no longer observed, not that the analysis is scientifically valid.
Use **Check Results** again and inspect the complete stage before accepting it.
The original execution remains a failed experiment record; each correction has its own record linked to the source evidence.

Corrections stop when evidence is missing or changed, the conversation is redirected, or an attempt or work limit is reached.
They do not silently restart after reopening a session.
If an assessed attempt has remaining consent and budget, Heartwood may offer **Continue Corrections**; inspect the retained results before continuing.
Pause active work before cancelling the workflow.

### Verify and Accept Results

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
