<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Research Specialists

A research specialist gives the agent a focused second pass on one part of a research task and returns its result to the conversation.

## Ask for a Specialist Review

Name the review you want in your request:

```text
Before changing files, ask the Research Planner to identify the inputs, assumptions, and verification steps. Then implement only the approved plan and ask the Reproducibility Reviewer to assess the final evidence.
```

When the agent delegates, the delegation appears as a **Specialist Review** action in the pending action set.
Check the specialist name and the delegated objective before allowing the set; no review mode allows a specialist task without you.
When the specialist finishes, its result returns to the agent, which decides how to continue.

Heartwood runs one specialist at a time by default and records its lifecycle, lineage, usage, and result in the session that every interface shows.
Each delegation is a fresh review of the evidence the agent supplies; ask for another review when the evidence changes.

Use **Pause** in the browser, `/pause` in the terminal, or `session.pause()` in a notebook to request cancellation of active specialist work.
Cancellation does not undo completed actions, and the model provider may still process and bill a request it already accepted.

## Available Reviews

| Specialist | Focus | Verified Skills |
|---|---|---|
| Research Planner | A sequential plan, assumptions, inputs, checks, and required evidence | None |
| Data Quality Reviewer | Completeness, validity, temporal consistency, duplicates, and denominators | `omop-cohort-summary` |
| Cohort and Feature Reviewer | Eligibility, timing, censoring, feature construction, and leakage | `omop-cohort-summary` |
| Statistical Reviewer | Estimands, splits, specification, uncertainty, validation, and interpretation | `baseline-model` |
| Reproducibility Reviewer | Revisions, environments, parameters, execution, artifacts, and verification | `omop-cohort-summary`, `baseline-model`, `aggregate-export` |

Specialists are advisory.
They see only the evidence the agent passes to them and cannot read files, run commands, use the network, or change the project.
Each has a model-step limit and usage budget, uses the active model route, and can use only the verified Skills listed for it.
In a deployment with an isolated worker, a specialist runs inside its parent's worker process and sandbox, with the parent's model route.
It is not independently isolated: the sandbox, not the specialist's missing tools, is the boundary for anything that runs in that worker.

The catalog also lists **Analysis Implementer** as not available.
A specialist with project tools would need its own actions to appear in your review and to recover safely after a restart, which the OpenHands task interface does not provide.

## See the Catalog

- Terminal: `heartwood specialists` outside a session, or `/specialists` inside one.
- Browser: **Specialists** in the session navigation.
- Notebook: `session.specialist_settings()`.

![Heartwood Specialists panel listing the advisory roles with their step limits and Skills](../assets/screenshots/browser-specialists-light.png#gh-light-mode-only){ .theme-screenshot-light }
![Heartwood Specialists panel listing the advisory roles with their step limits and Skills](../assets/screenshots/browser-specialists-dark.png#gh-dark-mode-only){ .theme-screenshot-dark }

## Read Specialist Results

Specialist output is model-generated review, not independent scientific validation.
Inspect the evidence and apply the same domain, statistical, and reproducibility review as for work you produced yourself.

In a [research workflow](research-workflows.md), specialists also return structured findings, shown as **Unverified review proposals** in every interface.
A proposal names something to check; it does not establish a defect or authorize a correction, and no proposals is not proof that the analysis is correct.
If a specialist cannot supply its required structured result, Heartwood reports the task as failed instead of accepting the summary.
A deployment can also offer a qualified [parallel review](research-workflows.md#parallel-reviews) for workflow stages with several reviewers; it gives specialists no additional tools.

The audit export records specialist lifecycle and usage, not the delegated instructions or returned text.
