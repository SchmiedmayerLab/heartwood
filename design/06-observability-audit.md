<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 06 — Observability, audit, and feedback

## The audit record

The agent core is event-sourced: every agent message, tool call, model call, and user input is an immutable event appended to the agent-server log persisted on the workspace disk (via the SDK `FileStore`). heartwood keeps a separate hash-chained **audit log derived from the translated session events** — execution substrate versus compliance record. This yields deterministic replay, pause/resume/fork, and export without extra machinery. Per session, heartwood records:

- **Model calls** — endpoint, token counts, policy decision, latency (content is not logged by default).
- **Tool + code execution** — tool, command/code, exit status (data values are referenced, not copied).
- **Skill activations** — skill/sub-agent, detection evidence and confidence, and the human approval that authorized it.
- **Egress decisions** — every allowed and denied network attempt.
- **Human interactions** — confirmations, rejections, manual picks.

This one record serves reproducibility, the egress attestation, and debugging.

## Tamper-evidence

Each event carries the hash of the prior event (a chained log), so any retroactive edit breaks the chain and is detectable; the chain head is checkpointed. Exports are Sigstore-signed, and an authoritative off-VM copy makes local tampering moot. Where a researcher has root on the VM, local immutability cannot be guaranteed — hash-chaining makes tampering **detectable** and signed export provides the authoritative record; this is documented rather than overclaimed.

## Researcher-facing activity view

The CLI, notebook, and researcher web UI render the same plain-language trace: "detected OMOP (0.82) → loaded `omop-cohort-builder` (approved) → wrote and ran this query in the sandbox → called Claude on Vertex (in-perimeter) → returned aggregate counts." This is what makes the system inspectable for a non-technical user without reading raw logs.

## Export and improvement loop

Field experience improves skills, prompts, and detection **without moving PHI**:

1. **Consented, researcher-initiated export** — no silent telemetry.
2. **PHI scrub + reduction** — the export keeps the *trajectory skeleton* (task, decision sequence, model reasoning, errors/recovery, timings, quality ratings) and strips data values; a scrubber redacts PHI-shaped content.
3. **Human validation gate** — the researcher (and a reviewer where required) confirms the artifact is PHI-free and aggregate-only before it leaves the perimeter, mirroring normal result-export review.
4. **Out-of-band aggregation** — validated trajectories become replay tests ([07](07-testing-eval.md)), drive skill/detector fixes, and refine prompts/policy.
5. **Improvements ship back** as signed skills / a new image — closing the loop with no PHI ever leaving the boundary.

In a fully air-gapped site, export is a manual, reviewed file carried out under the platform's existing export controls; its value is that the artifact is already structured for improvement.

## Anti-goals

No hidden telemetry; nothing leaves the perimeter without explicit, reviewed, consented export; no raw-PHI logging by default; no network service required to produce or store the audit trail.
