<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 04 — Skills and auto-detection

## Format: `SKILL.md`

Skills use the open **`SKILL.md`** standard (a directory with a `SKILL.md` file plus optional `scripts/`, `references/`, `assets/`), loaded natively by the agent core with progressive disclosure: only ~100 tokens of name+description per skill load at startup; the body loads on activation; resources load on reference. The standard is portable across agents (Claude Code, Codex, Gemini CLI, Cursor), so a skill authored for heartwood remains valid elsewhere, which preserves author reach.

## heartwood metadata

Skills carry a namespaced `heartwood.*` block in the `metadata` field, so they stay valid in any `SKILL.md` host while heartwood enforces the load-bearing fields in code (never via model self-report):

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

## Auto-detection (deterministic, propose-not-commit)

Detection is a fast, auditable, offline-testable pipeline that **proposes** — it never silently loads a skill or runs code. The danger it guards against is guessing wrong *authoritatively* for a non-technical user.

1. **Platform** — env-var / file probes (`WORKSPACE_*`/`GOOGLE_PROJECT` → Terra; `DX_*` → DNAnexus; SB config → Seven Bridges; else generic). Pure code, no tokens.
2. **Dataset** — schema/format fingerprints (OMOP table-name set in `INFORMATION_SCHEMA`; VCF/BAM/DICOM magic bytes; FHIR NDJSON), each with a confidence.
3. **Selection** — a manifest maps `(platform, dataset) → skills` from the metadata above; a dispatcher emits a visible, logged proposal with evidence and confidence ("Detected DNAnexus + VCF (0.95) → propose `genomics-qc`, `vcf-cohort-filter` [confirm] [pick] [why]"). Low confidence requires confirmation; "detection is wrong / let me pick" is a one-click primary path. Embedding retrieval is used only for the long tail (undeclared or ambiguous cases), always followed by confirmation.

The common path uses no model call and is unit-tested against synthetic fixtures of each platform + dataset. Only matched skills' metadata enters context at start; full bodies load on activation, keeping skill overhead small.

## Sub-agents

Sub-agents are specialized skill+tool bundles (e.g. `genomics-qc`, `omop-phenotyping`) with scoped context, packaged the same way and delegated to via the SDK. The detector may propose one for a detected dataset.

## Sharing and distribution

- **Standard format, heartwood verification gate.** heartwood does not run a competing general registry. It **curates and signs** a secure-platform skill collection, **aggregates** existing registries (e.g. BioContextAI), and cross-publishes generic skills upstream so authors reach the larger audience.
- **Bundle catalog.** Phase 0 tracks supported packaged skills in `skills/bundle.toml`. Local entries point at checked-in `SKILL.md` directories and resolve through the local verification gate. External git entries are import specifications: semver tag, resolved commit, content SHA-256, repository-relative path, `heartwood.*` metadata, and Sigstore provenance. The build-time importer vendors external entries into the same local skill directory shape before runtime.
- **Offline-first.** At build time (outside the perimeter), the image build clones skills at a pinned semver tag, verifies signatures, and vendors them into a read-only directory the agent core loads from. No runtime network is required or allowed. Large binary assets ship as OCI/ORAS artifacts resolved at build.
- **Versioning.** Semver is mandatory (never commit-SHA-as-version) so builds pin reproducibly and reviewers can validate a specific version. Release channels (`stable` = verified; `latest` = community) are separate refs.
- **Trust tiers.** `verified` (reviewed + signed, pre-seeded), `community` (installable, badged), `experimental` (opt-in only). Enforcement — signing, sandboxing, scanning, approval UX — is in [05](05-security-compliance.md).
