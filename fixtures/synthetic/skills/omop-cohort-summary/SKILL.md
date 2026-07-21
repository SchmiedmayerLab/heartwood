---
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
name: "Synthetic OMOP cohort summary"
description: "Define an aggregate target-condition cohort from synthetic OMOP-like fixture tables."
metadata:
  heartwood.dataset-types: "omop-cdm"
  heartwood.platforms: "generic,terra"
  heartwood.phi-risk: "none"
  heartwood.trust-tier: "verified"
  heartwood.requires-network: "false"
  heartwood.version: "0.2.0-beta.4"
  heartwood.sig: "sigstore:synthetic-fixture"
---

# Synthetic OMOP Cohort Summary

This metadata-only synthetic fixture exercises schema and fixture-lint checks independently from the executable repository-verified Skill bundle.
