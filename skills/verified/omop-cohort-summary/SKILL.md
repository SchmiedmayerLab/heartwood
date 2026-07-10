---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
id: "heartwood.synthetic.omop-cohort-summary"
name: "omop-cohort-summary"
description: "Build aggregate cohort counts and quality checks from synthetic OMOP-like tables."
tools: "read-local-csv,write-aggregate-json"
approval-summary: "Reads synthetic OMOP-like CSV tables from the configured local data root and writes aggregate counts plus quality checks without row values."
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

# Synthetic OMOP Cohort Summary

Summarizes the checked-in synthetic OMOP-like `person` and `condition_occurrence` tables. The script emits aggregate counts, basic referential quality checks, and an exportability flag derived from the configured aggregate count floor.
