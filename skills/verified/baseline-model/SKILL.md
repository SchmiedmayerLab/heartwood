---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
id: "heartwood.synthetic.baseline-model"
name: "Synthetic baseline model"
description: "Train a deterministic baseline model artifact over synthetic OMOP-like tables."
tools: "read-local-csv,train-synthetic-baseline,write-aggregate-json"
approval-summary: "Reads synthetic OMOP-like CSV tables and writes a deterministic baseline model artifact without row-level values."
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

# Synthetic Baseline Model

Builds a deterministic baseline model artifact from synthetic OMOP-like tables. The output records model structure and quality checks without exporting row-level values.
