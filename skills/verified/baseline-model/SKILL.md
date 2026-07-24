---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
id: "heartwood.synthetic.baseline-model"
name: "baseline-model"
description: "Fit a deterministic age-only logistic baseline over a synthetic OMOP condition-history outcome."
tools: "read-local-csv,train-synthetic-baseline,write-aggregate-json"
approval-summary: "Reads synthetic OMOP-like CSV tables and writes aggregate training diagnostics for an age-only logistic baseline without row-level values."
entrypoint: "scripts/run.py"
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "generic,terra"
  heartwood.phi-risk: "none"
  heartwood.trust-tier: "verified"
  heartwood.requires-network: "false"
  heartwood.version: "0.2.0"
  heartwood.sig: "sigstore:synthetic-fixture"
---

# Synthetic Baseline Model

Use this Skill only after inspecting the target-condition cohort and data-quality results. It fits a dependency-free age-only logistic model for recorded target-condition history and emits aggregate training diagnostics without row identifiers or predictions.

The model is deliberately a baseline. Its Brier score and ROC AUC are measured on the training fixture, no holdout evaluation is performed, and the result is not a clinical prediction model or capability claim. Compare future models against it only with a separately reviewed evaluation design.

Use the exact Skill directory reported by `invoke_skill` to run the entrypoint; do not resolve `scripts/run.py` from the project directory.

Example:

```bash
SKILL_DIR=/exact/directory/reported/by/invoke_skill
python "$SKILL_DIR/scripts/run.py" \
  --data-root data \
  --target-condition-concept-id 201826 \
  --as-of-year 2025 \
  --output baseline-model.json
```
