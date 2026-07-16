---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
id: "heartwood.synthetic.aggregate-export"
name: "aggregate-export"
description: "Apply the configured aggregate count floor before exporting a synthetic cohort summary."
tools: "write-aggregate-json"
approval-summary: "Reads a synthetic cohort summary artifact and writes only aggregate outputs that satisfy the configured count floor."
entrypoint: "scripts/run.py"
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "generic,terra"
  heartwood.phi-risk: "none"
  heartwood.trust-tier: "verified"
  heartwood.requires-network: "false"
  heartwood.version: "0.2.0-beta.2"
  heartwood.sig: "sigstore:synthetic-fixture"
---

# Synthetic Aggregate Export

Use this Skill only on a reviewed cohort-summary artifact. It applies the configured participant-count floor and writes either aggregate counts or a suppression decision. A successful script result is not permission to move the file out of the workspace; platform and institutional export authorization remain separate.

Example:

```bash
python scripts/run.py \
  --summary cohort-summary.json \
  --aggregate-count-floor 20 \
  --output aggregate-export.json
```
