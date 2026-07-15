---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
id: "heartwood.synthetic.omop-cohort-summary"
name: "omop-cohort-summary"
description: "Define a target-condition cohort and report aggregate quality checks from synthetic OMOP-like tables."
tools: "read-local-csv,write-aggregate-json"
approval-summary: "Reads synthetic OMOP-like CSV tables from the configured local data root and writes aggregate counts plus quality checks without row values."
entrypoint: "scripts/run.py"
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "generic,terra"
  heartwood.phi-risk: "none"
  heartwood.trust-tier: "verified"
  heartwood.requires-network: "false"
  heartwood.version: "0.2.0-beta.1"
  heartwood.sig: "sigstore:synthetic-fixture"
---

# Synthetic OMOP Cohort Summary

Use this Skill when a researcher asks for a reproducible target-condition cohort over localized OMOP-like `person` and `condition_occurrence` tables.

1. Confirm the local data root and target condition concept identifier. Do not infer a clinical label from an identifier.
2. Run `scripts/run.py` with explicit input and output paths. The default synthetic reference concept is `201826`, minimum age is 18 years at first target occurrence, and aggregate count floor is 20.
3. Report the cohort definition, inclusion and exclusion counts, age-at-index summary, and every data-quality check before interpreting the result.
4. Treat the output as an in-boundary aggregate artifact. Do not claim that it is clinically validated or representative of a complete OMOP Common Data Model cohort implementation.

Example:

```bash
python scripts/run.py \
  --data-root /path/to/localized/omop \
  --target-condition-concept-id 201826 \
  --minimum-age 18 \
  --aggregate-count-floor 20 \
  --output cohort-summary.json
```
