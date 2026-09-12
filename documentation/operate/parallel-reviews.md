<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Qualify Parallel Reviews

Parallel review lets independent advisory specialists examine the same analysis concurrently.
It does not authorize file changes, replace the parent agent, or relax action review.
Use it only when retained trials demonstrate useful speed improvement without lower review quality or excessive resource use.

## Establish Evidence

Use the [research evaluation harness](../architecture/testing.md) in dedicated synthetic projects.
Run matched sequential and parallel trials against the maintained planning-review suite, with the same model revision, runtime, Skills, fixtures, seed, and work budget.
Retain complete `EvaluationRun` records, including failed and interrupted trials.
The default gate requires three matching repetitions per case, fresh evidence, all required checks, known usage and cost, at least a 10% latency improvement, and no more than 25% additional tokens or reported cost.
Deterministic tests cannot qualify a real provider.

The typed `ReviewQualification` contract contains the suite, covered case, sequential and parallel configurations, policy, and retained records.
`ReviewQualifications` groups approved route entries in one JSON document.
Construct these models from the existing harness records; do not manufacture successful checks or set an eligibility flag.
Two entries matching the same runtime and stage are ambiguous and will be rejected.

The gateway verifies the observed SDK version, model options, policy, context limits, scoped tool behavior, and specialist catalog against the retained runtime fingerprint.
Remote model revision, hardware, precision, Skill-tree and harness revision declarations still require operator verification.
Changing the deployed build or a remote service behind an unchanged model name requires reassessment; the fingerprint is not remote-server or binary attestation.

## Configure the Gateway

Install the JSON file outside every Heartwood project, in an operator-controlled location such as `/etc/heartwood/review-qualifications.json`.
It must be a regular file, not a symbolic link, and must not be writable by group or other users.
Mount it read-only in containers and deny the agent execution identity write access using platform controls.
Supply its absolute path when starting Heartwood:

```sh
export HEARTWOOD_REVIEW_QUALIFICATIONS=/etc/heartwood/review-qualifications.json
heartwood
```

Use the same environment setting for the browser server or notebook kernel.
Do not store this configuration in `.heartwood/` or expose a browser upload for it.
Embedding applications may instead inject the existing trusted `parallel_review_preparer`; configuring both sources is rejected.

!!! warning "Evidence is an operator trust decision"
    The file contains retained measurements, not a cryptographically authenticated qualification certificate.
    A schema-valid file is not proof that a trial occurred.
    Review the originating run and preserve its provenance before installing it.
    An external path and file permissions do not isolate an agent running as the same operating-system user; use a separate identity or read-only mount for maintained deployments.

## Review and Revoke

The researcher selects **Preview Parallel Review** and reviews the exact workers and work limits before consenting.
Normal grouped tool approval is still required.
The gateway re-reads evidence and checks runtime ownership during preparation and native dispatch; it does not use a cached success flag.
Offered limits cannot exceed the maintained stage or the retained case budgets.

Remove the file to prevent new parallel admissions, or replace it atomically with reviewed evidence.
Stale, missing, malformed, ambiguous, or changed evidence cannot authorize new work.
After an update, refresh the preview and obtain new consent when its identity changes.
Removing evidence does not undo completed actions or forcibly stop already admitted work; use the normal pause or cancellation controls for active work.
Sequential review remains available through **Review Analysis**.
