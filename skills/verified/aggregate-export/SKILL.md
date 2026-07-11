---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
id: "heartwood.synthetic.aggregate-export"
name: "aggregate-export"
description: "Apply the aggregate count floor before exporting synthetic cohort summaries."
tools: "write-aggregate-json"
approval-summary: "Reads a synthetic cohort summary artifact and writes only aggregate outputs that satisfy the configured count floor."
entrypoint: "scripts/run.py"
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "generic"
  heartwood.phi-risk: "none"
  heartwood.trust-tier: "verified"
  heartwood.requires-network: "false"
  heartwood.version: "0.1.0"
  heartwood.sig: "sigstore:synthetic-fixture"
---

# Synthetic Aggregate Export

Reads a cohort summary artifact and produces an export decision. If the participant count is below the configured floor, the output records suppression without writing the suppressed count.
