<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 04 — Skills And Auto-Detection

## Format: `SKILL.md`

Skills use the open **`SKILL.md`** standard (a directory with a `SKILL.md` file plus optional `scripts/`, `references/`, `assets/`), loaded natively by the agent core with progressive disclosure: name and description load at startup, the body loads on activation, and resources load on reference. The standard is portable across common agent and editor hosts, so a Skill authored for Heartwood remains valid elsewhere.

## Heartwood Metadata

Skills carry a namespaced `heartwood.*` block in the `metadata` field, so they stay valid in any `SKILL.md` host while Heartwood enforces the load-bearing fields in code rather than through model self-report:

```yaml
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "terra,dnanexus,generic"
  heartwood.phi-risk: "reads-phi"        # none | reads-phi | writes-outside-boundary
  heartwood.trust-tier: "verified"       # verified | community | experimental
  heartwood.requires-network: "false"
  heartwood.version: "1.3.0"
  heartwood.sig: "sigstore:<bundle-ref>"
```

## Auto-Detection

Detection is a fast, auditable, offline-testable pipeline that reports evidence and confidence without executing code, changing installed Skills, or asserting an unsupported dataset identity. Repository-verified bundled Skills remain available to OpenHands; detection will eventually narrow and rank that bundle for the active platform and dataset.

1. **Platform** — env-var / file probes (`WORKSPACE_*`/`GOOGLE_PROJECT` → Terra; `DX_*` → DNAnexus; SB config → Seven Bridges; else generic). Pure code, no tokens.
2. **Dataset** — the current integration fixture produces a deterministic synthetic OMOP fingerprint. A normal unconfigured runtime must report no detected dataset until a real data-source adapter supplies schema or format evidence such as OMOP table-name sets in `INFORMATION_SCHEMA`, VCF/BAM/DICOM magic bytes, or FHIR NDJSON structure.
3. **Selection target** — the dispatcher maps `(platform, dataset) → Skills` and emits a visible, logged proposal with a researcher correction path. This dispatcher is not implemented; the runtime currently makes the small checked-in bundle available to OpenHands without dataset filtering. Community or experimental Skills require installation-time approval before they can enter the runtime Skill directory.

Detection uses no model call. OpenHands receives repository-verified Skill metadata at startup and loads full bodies on activation through native progressive disclosure. Dataset-aware narrowing remains future work and must not bypass the extension trust gate.

## Current Packaging And Trust

- **Current bundle.** `skills/bundle.toml` selects checked-in `SKILL.md` directories. The repository gate validates metadata consistency, declared tools, network posture, entrypoint confinement, deterministic tests, and provenance-field shape before the image includes them. OpenHands loads the resulting read-only bundle without runtime network access.
- **Reference analysis.** `omop-cohort-summary` defines an adult target-condition cohort and aggregate quality checks, `baseline-model` fits an age-only logistic baseline for recorded condition history and reports training diagnostics without holdout claims, and `aggregate-export` suppresses results below the configured count floor. These Skills are deterministic synthetic integration implementations; their outputs are not clinical, statistical, export, or institutional approval.
- **Current trust meaning.** `verified` means repository-verified: the Skill is checked in, selected by the bundle, and accepted by local validation and tests. Existing `heartwood.sig` values are provenance placeholders; the repository does not perform cryptographic Sigstore verification. Repository verification is not clinical, statistical, security, or institutional approval.
- **Extensions.** `community` and `experimental` Skills can be installed only from a mounted local directory after explicit review. Heartwood does not fetch external Skills or maintain a parallel registry at runtime.

## Runtime Extension Installation

`heartwood skills inspect <mounted-directory>` verifies and displays one extension before installation. `heartwood skills install <mounted-directory> --approve` records the installation-time decision, rejects unsupported tools, required network access, malformed metadata, path escapes, symbolic links, and bundled-name replacement, and atomically copies the source into deployment-persistent Skill storage. `heartwood skills remove <name>` removes only installed extensions. The web Skills panel exposes the same inspect, approve, install, list, and remove operations through the gateway.

The gateway gives OpenHands both the read-only bundled directory and the persistent installed directory. OpenHands performs native progressive disclosure across both. Automatic user, public-marketplace, and project-workspace Skill loading is disabled so a workspace cannot bypass installation review or override a repository-verified Skill. Installed Skills do not create a second activation protocol and do not trigger repeated conversational approval.

## Future Distribution

External imports require a build-time workflow that resolves an immutable source, verifies content digests and real signatures, records review provenance, vendors the result, and tests the final image. Release channels, cross-publication, dataset-aware selection, and sub-agent bundles remain future work until those controls exist.
