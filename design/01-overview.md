<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 01 — Overview

## What heartwood is

heartwood is a Docker-packaged coding harness for sensitive biomedical research data. It runs inside secure research platforms, executes generated analysis code next to the data, and keeps participant-level data inside the platform boundary.

The system owns the platform, policy, skills, and audit layer around an existing agent core: adapter-based platform detection, data-source fingerprinting, curated and signed skills, deny-egress model routing, aggregate-export controls, tamper-evident logs, and compliance exports.

## What it is not

- Not a new agent framework or agent loop — it builds on the OpenHands Software Agent SDK and agent-server behind a stable facade.
- Not a reuse of the OpenHands web UI — heartwood ships its own researcher-focused surfaces (CLI, notebook, and web UI) over one shared session contract.
- Not a new general skills registry — it curates and signs skills and aggregates existing registries.
- Not a way to send controlled-tier participant-level data to external model APIs.
- Not a batch-workflow engine — it *emits* CWL/WDL/Nextflow; it does not replace Cromwell or Nextflow.

## Users

- **Analyst-researcher (primary, often non-technical).** Gets a guided, low-configuration experience: detection proposes a vetted workflow, they confirm in plain language, and they never need to read code to stay safe. The outward-facing experience is a heartwood-owned researcher web UI or notebook widgets in Terra, backed by the same session model as the CLI.
- **Methods-developer (technical).** Gets open-ended agentic autonomy, full event-log inspection, skill authoring, and the primary CLI for development, testing, automation, and reproducible debugging.
- **Platform / IT reviewer.** Consumes the compliance kit, egress attestation, and audit log; approves once per site.

## Scope

**In:** the analysis loop (NL → code → sandboxed execution → aggregate results) on structured biomedical data, starting with OMOP-on-BigQuery; a CLI-first interaction model with notebook and researcher-web-UI adapters over one shared session contract; in-boundary model access; auto-detection; skill loading, curation, signing, and offline bundling; audit and compliance artifacts; a platform-agnostic core with adapters.

**Deferred:** fully autonomous use of weak/local models, external durable-execution engines, and batch-workflow emission. Multiple platforms are supported from the start via adapters; deep real-data validation is added incrementally.

## Reference workflow

A researcher launches the image on their platform and starts a session from the CLI, a notebook widget, or the researcher web UI. In one guided flow, they perform **reproducible in-perimeter cohort extraction → QC → a baseline model**, producing an **egress-attestation report** suitable for an IRB. This is the canonical end-to-end capability the platform delivers.
