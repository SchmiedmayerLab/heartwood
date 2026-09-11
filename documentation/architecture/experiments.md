<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Experiment Records

Scientific execution records describe declared code, inputs, parameters, environment, and outputs.
They are separate from the [session audit](sessions-audit.md), which records agent decisions and action permissions.

## Research Workflow Records

[Research workflows](../use/research-workflows.md) record each stage before asking the agent to work.
The definition binds the stage to its workflow version, input fingerprints, previously accepted artifacts, and declared output paths.
Generated Python artifacts are identified as code, so a later verification stage can reference the exact program accepted earlier.

A stage receives a successful record only after its independent checks and any required researcher review pass.
The outcome links to the recorded tool proposals, action decisions, tool results, and reproduction evidence for that stage.
It does not invent a process exit code: stage acceptance and an individual command's exit status are different observations.
Cancelling a started stage records cancellation; cancelling before a stage starts does not imply execution occurred.

### Correction Lineage

Corrective work preserves the original stage execution as failed and records each new attempt separately.
Its definition contains the correction attempt identity, original reviewed inputs and code, and fresh output paths.
The workflow retains the verified findings, exact source snapshot, permitted attempt count, and each independent recheck.
It shares the original stage's usage and deadline; starting another attempt does not reset either limit.

A correction experiment succeeds only when its declared replacement files pass the supported defect rechecks.
This narrower outcome does not mean the complete workflow stage passed: the normal stage checks and researcher gate still apply.
Only independently checked replacement paths enter the current stage's artifact binding, and previously accepted stages are unchanged.
The original failed record is never rewritten as a successful execution.

Attempt intent and experiment start are journaled before model dispatch.
Recovery can finish an interrupted assessment from recorded evidence but cannot silently repeat execution or begin the next attempt.
Tool proposals still use the existing action-review policy; correction consent is not approval of a command or file modification.

### Inspect and Export

Inspect **Experiment Records** in the browser's Research tab, enter `/experiments` in the terminal, or read `NotebookViewModel.experiments` in a notebook.
All three use the same gateway-owned session projection.
The notebook widget also includes an Experiment Records section.
For all sessions and recorded scripts in the project, expand **Project Experiment Records** in the browser's Research tab.
**Refresh** reads current project state; **Export Records** downloads the gateway's canonical JSON Lines and displays its digest.
The export is project-local scientific metadata, not a signed audit checkpoint.
The notebook exposes the same collection and export through `session.experiment_records()` and `session.export_experiments()`.

The start and outcome are committed with their workflow transitions in the existing paired session and audit journal.
Their content-minimized fingerprints are included in the security audit; the full scientific records remain project-private.
Project queries rebuild `.heartwood/experiments.jsonl` from those verified source events using idempotent appends.
Rebuilding never reads newer artifact contents or repeats model calls or tool actions.

The gateway exposes `experiment_records()` and `export_experiments()` for project-wide inspection and export.
The corresponding authenticated HTTP routes are `GET /research/experiments` and `GET /research/experiments/export`.
An export includes canonical JSON Lines and its SHA-256 digest; it is not a signed audit checkpoint.
The project collection also includes analyses explicitly recorded through the Python API and command-line recorder below.

## Record a Command-Line Analysis

From your project directory, declare the input and new output files before the script name:

```bash
heartwood experiments record --input data.csv --output results.json analysis.py --seed 42
heartwood experiments list
heartwood experiments export > experiment-records.jsonl
```

Heartwood uses its installed Python by default and passes everything after `analysis.py` literally to the script.
Use repeated `--input`, `--output`, and `--code` options for additional files; the script itself is always included as code.
Output directories must already exist, and declared output files must not exist.
The command prints a run identity and leaves the analysis's output visible in the terminal without copying it into the journal.
It returns the script's nonzero exit status when execution fails; a zero exit alone is not success if files changed or declared outputs are missing.

This is an explicit user-run analysis, not an autonomous OpenHands tool or a sandbox.
The script has the current user's ordinary filesystem and network permissions.
Use normal Heartwood conversation actions when an agent proposes executing code so that the action-review policy applies.

For another interpreter, use `--runner bash` or `--runner Rscript` and supply `--environment-sha256` with the complete digest of your maintained environment specification.
That fingerprint is a declaration, not proof that the external interpreter matches it.
Heartwood does not substitute its own Python environment as evidence for another interpreter.
For example, a shell script can be recorded with:

```text
heartwood experiments record --runner bash --environment-sha256 DIGEST --input data.csv --output results.json analysis.sh
```

Replace `DIGEST` with the actual 64-character SHA-256 digest; do not use a placeholder as reproducibility evidence.
The same file-boundary checks and versioned record schema apply to Python and command-line analyses.
Raw arguments, environment-variable values, and process output are not stored in the record.
Arguments may still appear in shell history or the operating system's process list; never pass credentials or protected values this way.

`heartwood experiments list --json` returns the gateway's project collection schema.
`heartwood experiments export` writes verified canonical JSON Lines to standard output; protect the exported file like other project metadata.
For an abandoned script, `heartwood experiments recover RUN_ID` and `heartwood experiments cancel RUN_ID` follow the [recovery rules](#recover-an-interrupted-script) below without stopping processes or deleting files.
Use `--run-id RUN_ID` when automating an initial command whose retries must retain one identity: reuse is rejected rather than repeating work.

## Record a Python Analysis

Use the Python recorder around the call that runs your analysis.
Run from the project directory, declare every relevant input and code dependency, and choose output files that do not already exist.
Create their parent directories before recording.

```python
from heartwood.gateway import ProjectContext
from heartwood.gateway.experiments import ExperimentRecorder

recorder = ExperimentRecorder(ProjectContext.current())
definition = recorder.describe(
    actor_ref="researcher-001",
    entry_point="analysis.py",
    inputs=("data.csv",),
    outputs=("results.json",),
    parameters={"seed": 42},
)

with recorder.record(definition) as run_id:
    from analysis import main

    main(seed=42)

run = next(item for item in recorder.runs() if item.run_id == run_id)
print(run.status)
print(run.outputs)
```

The recorder observes the declared files before entering the block and checks that inputs and code remain unchanged afterward.
If the Python environment was observed, its fingerprint must also remain unchanged.
Successful completion requires every declared output to exist and be readable.
Missing outputs, changed inputs, and exceptions produce a failed record; a keyboard interruption produces a cancelled record.
The original exception is propagated without putting its text in the journal.
The recorder does not execute the script, supply parameters to it, or infer undeclared dependencies.
The caller remains responsible for connecting the declaration to the actual analysis call.

File hashing uses bounded, streaming reads through the same project boundary as workspace inspection.
The default limit is 1 GiB per file; pass `max_file_bytes` to `ExperimentRecorder` to change it deliberately.
Hashing large files requires reading their bytes and can add noticeable I/O time on shared storage.

## Record Contents

The versioned `ExperimentEvent` schema in `heartwood.schemas.experiments` is language-neutral.
Its shared reducer produces `ExperimentRun` summaries.

| Field | Meaning |
|---|---|
| Run and event identifiers | Stable identities for execution and idempotent record appends |
| Actor reference | A caller-declared identifier, not authenticated proof of identity |
| Code and input files | Project-relative paths, SHA-256 digests, and byte counts |
| Parameters and invocation | Digests of the explicit declarations, without raw values or arguments |
| Environment | A digest and whether its description was observed or declared |
| Git revision and cleanliness | Optional explicit context; unknown values remain unset |
| Workflow stage | Optional session, workflow run, definition fingerprint, stage, and tool association |
| Evidence links | Source session-event identities, kinds, and fingerprints, without commands or results |
| Outcome | Attempt number, timestamps, status, exit code when known, and observed outputs |

The default Python environment digest covers interpreter identity, operating-system family, machine architecture, and installed package names and versions.
It excludes installation paths, package source URLs, credentials, and environment-variable values.
It does not capture external executables, native libraries, a GPU driver, or the complete contents of an installed package.
For research stages, it describes the gateway's Python environment, not a remote model server or an independently attested execution environment.
An explicit `ExperimentEnvironment` can instead identify a declared container or environment description.

## Persistence and Recovery

Records live in `.heartwood/experiments.jsonl` and use the existing private JSON Lines append journal and native file locks.
Project initialization registers the experiment formats in `.heartwood/state.json` without rewriting existing session, audit, or experiment records.
Supported older project manifests are upgraded atomically; unknown or altered format declarations are rejected rather than relabeled.
A hash chain detects modification or reordering within the available record history.
Exact event retries do not append a second record; changed retries and invalid transitions are rejected.
One shared reducer supplies summaries and exact output-path/digest producer queries.

The Python recorder reserves a run before entering user code and holds its ownership lock until it exits.
Reusing a run identifier with `record()` never enters the code block again, even after process loss.
An unfinished `started` record means that execution may or may not have occurred; reading it does not repeat the work or infer success from files left behind.
An unfinished `resumed` record has the same uncertainty for its later attempt.
Terminal outcomes cannot be replaced.

### Recover an Interrupted Script

First check whether the original process or any subprocess it launched is still running, and inspect partial outputs and external effects.
`recorder.recover(run_id)` requires the recorder's ownership lock to be free and marks the attempt `interrupted` without executing anything.
It does not stop detached processes, remove files, or establish that retrying is safe.
Calling it again leaves the same interrupted record unchanged.

Choose one of these actions after inspection:

- `recorder.cancel(run_id)` closes the abandoned record without undoing or deleting anything.
- `recorder.resume(run_id)` explicitly starts another attempt under the same run identity and original declaration.

A resumed attempt requires unchanged code, inputs, and any observed Python environment.
Declared outputs must be absent; Heartwood never deletes partial results to make a retry possible.
The caller must establish that repeating any undeclared or external effects is safe and handle partial outputs deliberately.
Changed parameters or dependencies belong in a new experiment, not a resumed one.

After recovering and verifying that a retry is safe:

```python
with recorder.resume(run_id):
    from analysis import main

    main(seed=42)
```

The resumed intent is persisted before the block runs, and its attempt number increases while the original input declaration and start time remain unchanged.
An interrupted resume is not automatically retried.
These script APIs cannot recover, cancel, or replace gateway-owned workflow stage records; use the workflow's normal session controls for those.

`recorder.export()` returns a deterministic, verified JSON Lines snapshot.
An export is not a signed checkpoint or deployment-owned immutable record.
An operator can bind its exact bytes to the existing audit checkpoint with [`heartwood audit checkpoint --include-experiments`](../operate/audit-checkpoints.md#retain-experiment-records).
This retains a verified project snapshot separately from the content-minimized audit and uses the deployment's existing signer and retention controls.
The `ExperimentSink` contract separates ordered, idempotent appends from their storage implementation; the implemented backend is project-local storage.

## Evidence Boundaries

These records do not establish scientific correctness, independently authenticate the actor, or prove that a declared script generated an output.
Workflow actor references are hashed session-command actor identifiers, not independent identity attestations.
Boundary fingerprints detect ordinary changes, not adversarial change-and-restore between observations.
Someone who can rewrite project state can rewrite the records and recompute their chain; an intact suffix can also be deleted without an independently retained reference.

Paths, actor references, and metadata can be sensitive even when file contents are excluded.
Digests are not anonymization: a small set of possible parameter values can be guessed and hashed.
Keep protected values out of declarations, and apply the project's access and export policies to scientific records independently of the security audit.
