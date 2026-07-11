<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 05 — Security And Compliance

## Threat Model

Heartwood addresses protected-data exfiltration through model calls, tools, Skills, logs, or dependency fetches; malicious or careless extensions; direct and indirect prompt injection; credential exposure; unauthorized export; and plausible but incorrect analyses.

Heartwood does not make a trusted interactive container equivalent to a hardened multi-tenant sandbox. The platform owns the operating-system, network, identity, storage, and controlled-data boundary.

## Deployment Boundary

- Platform egress policy is the authoritative network control. Air-gapped deployments disable runtime networking; connected deployments allow only reviewed destinations through platform controls.
- Heartwood evaluates the selected model profile's declared normalized policy endpoint, capability tier, credential reference, and action-confirmation mode before initial task submission and before an approved or resumed continuation that may call the model. Rejecting a pending action does not continue the model. A custom base URL must share the policy endpoint's origin. Provider-native routing without a custom base URL remains an audited deployment assertion that authoritative platform network controls must enforce. Deployment policy defaults to the supervised tier and `always-confirm` mode only. This is a deny-by-default application gate and audit record, not a substitute for a firewall, private endpoint, or workspace sandbox.
- Model connections and profiles contain no secret values. Credentials are resolved at runtime from an environment variable, mounted file, or managed identity only after policy allows that non-secret reference. A user-submitted provider token may exist only in gateway memory for the running process; it is never returned to the browser, persisted in settings, written to logs or audit records, or accepted as a CLI argument. Policy records and audit exports never include the credential value.
- Model discovery is separately policy-gated egress. A deployment must authorize the exact catalog endpoint and credential reference before the gateway invokes a provider SDK. Catalog responses remain in the gateway's bounded memory cache and are not persisted to settings or session audit records; subsequent model-route records identify the selected non-secret profile and route decision.
- The active environment-referenced provider key is passed directly to the in-process OpenHands model client, while every environment-variable reference in the configured model profiles is blanked in OpenHands terminal subprocesses. This does not isolate mounted credential files or a managed identity from code running as the workspace user; those credentials require least privilege and a deployment-owned process, remote-workspace, or platform boundary when tools must not access them.
- Provider connections are configuration templates, not Health Insurance Portability and Accountability Act eligibility claims. The deploying institution must verify the business associate agreement, covered service, identity, region, retention, training, logging, and network path.
- Images contain no model weights. Optional artifacts are explicit runtime inputs in mounted or platform-persistent storage and are checked against an immutable source revision, byte size, SHA-256 digest, format, and license metadata.

## Human Decisions

Heartwood keeps three decisions separate:

1. Deployment authorization: administrators define permitted model and data routes. A researcher cannot override a denied route with a conversational click.
2. Extension trust: repository-verified Skills ship in the image and activate without repeated prompts; community or experimental Skills require one installation-time review before entering the runtime Skill directory.
3. Agent action confirmation: **Ask Every Time** uses OpenHands `AlwaysConfirm` and is the default. A deployment may also permit **Auto-Approve Low Risk**, which uses OpenHands `ConfirmRisky` at the `MEDIUM` threshold with unknown actions confirmed. Both modes use OpenHands deterministic policy-rail and pattern analyzers plus its model risk analyzer; Heartwood does not maintain its own classifier. Medium-, high-, and unknown-risk actions remain Allow once or Reject decisions in the CLI and web UI.

The OpenHands analyzers are defense in depth, not a sandbox, complete shell parser, prompt-injection solution, or hard-deny boundary. Heartwood invokes tools only through the OpenHands conversation loop because direct SDK tool execution bypasses analyzer and confirmation policy processing. `NeverConfirm` is not a researcher-facing option in the current interactive-container architecture.

Export remains an explicit researcher action under platform and dataset policy.

## Skill Trust

- Repository-verified Skills are checked in, included by the reviewed bundle catalog, and loaded through the OpenHands native `SKILL.md` loader.
- Repository verification identifies the Skill bytes shipped in the reviewed image; it does not make a writable same-user container immutable. A deployment that relies on runtime Skill integrity must keep application and bundled-Skill paths read-only or place tools in a separate workspace boundary.
- Current local verification checks metadata consistency, trust tier, declared tools, network posture, entrypoint confinement, and provenance-field shape. It does not cryptographically verify the existing Sigstore placeholders.
- Verified status requires independent security and clinical or statistical review before controlled-data use.
- External Skills are not fetched at runtime. The current installer accepts only a mounted local directory, rejects symbolic links and path escapes, validates declared permissions, records the trust decision, and copies atomically. Automatic OpenHands user, public-marketplace, and project-workspace Skill loading is disabled. Remote acquisition, immutable-source resolution, digest verification, and cryptographic signature verification remain future distribution controls.
- Skill instructions do not create process isolation. OpenHands local terminal and file tools start from the configured workspace inside the interactive container, but the local workspace is not a filesystem sandbox and same-user code can reach other readable paths. A deployment that needs stronger isolation must supply a supported remote workspace, operating-system sandbox, or platform-native job boundary and validate it independently.

## Data And Log Handling

- Source control, public examples, CI, replay traces, and screenshots use synthetic fixtures only.
- Researcher and agent message text remains in the in-boundary OpenHands conversation and Heartwood session event stores so every client can replay the same transcript, but exported audit records omit content by default.
- Heartwood creates each session directory with owner-only access and writes command, event, audit, and audit-export files with owner-only permissions. The deployment must independently protect the containing volume, OpenHands persistence, backups, and platform snapshots.
- Exported audit records capture route decisions, the selected action-confirmation mode, analyzer risk, confirmation results when requested, tool identity, exit status, Skill identity, and export metadata. Prompt and response content, model-generated action summaries, filesystem paths, row values, and secret values are scrubbed. The in-boundary session store retains the conversation and action summaries required for replay.
- Live protected health information must never be copied into fixtures, public logs, issue reports, or reviewer artifacts.
- Dependencies and repository-verified Skills are installed at image build time. Runtime package installation is not part of a normal researcher workflow.

## Audit And Tamper Evidence

Heartwood translates OpenHands events into one session stream and appends a separate hash-chained audit record. Hash chaining detects edits but cannot make a researcher-controlled local disk immutable. Authoritative retention, signing, and off-workspace copies require deployment integration and must be described as such.

## Compliance Evidence

Per-platform evidence should include the data-flow diagram, deployment policy profile, image and base digests, model route and credential-reference mechanism, artifact digest when local inference is used, network posture, identity binding, synthetic action-confirmation trace, scrubbed audit export, platform proxy behavior, and documented limitations.

The repository’s generated reviewer artifacts are a synthetic evidence aid, not a substitute for institutional security, privacy, clinical, or statistical review.

## Governance

The Stanford Schmiedmayer Lab maintains the repository. Named review ownership, release authority, security response, deprecation policy, and succession are required before controlled-data production use or external Skill distribution expands.
