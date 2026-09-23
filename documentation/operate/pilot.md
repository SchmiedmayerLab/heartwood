<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Run a Pilot Evaluation

A pilot shows whether a small group of researchers can install, use, and trust Heartwood in one standard environment before a wider rollout.
Run it with synthetic or approved non-sensitive data, a pinned release, and a named support contact.

## Prepare

1. Record the [standard research environment](standard-environment.md) that every participant uses.
2. Apply the [reviewed assistant](reviewed-assistant.md) controls.
3. Complete the synthetic checks in [Validate the Deployment](index.md#validate-the-deployment).
4. Prepare one synthetic dataset and data dictionary for all participants; see [Prepare the Inputs](../use/research-workflows.md#prepare-the-inputs).
5. Name a support contact and a feedback channel that does not collect session content.
6. Agree on exit criteria before the first participant starts.

## Onboard Each Participant

1. Create the environment from the recorded release and confirm `heartwood --version`.
2. Create a project directory and copy the synthetic inputs into it.
3. Run `heartwood doctor` from that directory and resolve every failed check.
4. Start Heartwood, select the approved model route, and allow or reject one proposed action.

Onboarding is complete when the participant has made one action decision that appears in the session history.

## Run the Pilot Task

Ask every participant to run the same [research workflows](../use/research-workflows.md) in order:

1. **Dataset Readiness Review** on the synthetic dataset.
2. **Reproducible Baseline Analysis** with the agreed research question.
3. **Independent Result Verification** of the baseline's script, outputs, and environment record.

Then run `heartwood audit verify` and `heartwood experiments list` in the project.

## Record Evidence

| Question | Evidence | Source |
|---|---|---|
| Could participants start? | Installation success and time to the first action decision | Onboarding notes |
| Did the workflows complete? | Outcome of each workflow run | `heartwood experiments list --json` |
| Did the result reproduce? | Verification outcome | Independent Result Verification report |
| Is the record intact? | Audit verification result | `heartwood audit verify` |
| What did support require? | Requests and time to resolution | Support log |
| Would participants use it? | Structured responses | Feedback form |

Collect counts and outcomes, not prompts, project files, or session history.
Keep pilot evidence with the deployment's records rather than in public issues.

## Change or Stop the Pilot

Keep the release pinned for the whole pilot.
Apply a security patch by following [Update the Environment](standard-environment.md#update-the-environment), and note the change in the evidence.

To stop, ask participants to keep the results they need, then delete the compute and decide whether each persistent disk should remain.

## Close the Pilot

Compare the evidence with the exit criteria and list the changes required before a wider rollout or a second platform.
Report pilot results as live validation in that environment; they do not establish institutional approval for other data or deployments.
See [Testing and Evidence](../architecture/testing.md#claims).
