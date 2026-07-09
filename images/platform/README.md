<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform-Derived Heartwood Images

The platform image layer builds Heartwood inside notebook images that must inherit behavior from a controlled research platform. The current implemented platform target is Terra. Future Seven Bridges and DNAnexus targets should use the same Dockerfile, manifest, and Bake structure after their base images and proxy behavior are validated.

The platform Dockerfile keeps the platform base image as the final stage, installs a managed Python 3.12 Heartwood environment under `/opt/heartwood/.venv`, registers a Heartwood Jupyter kernel when the platform exposes a Jupyter prefix, preserves `/opt/heartwood/docs/terra-jupyter-demo.ipynb`, and serves the Heartwood web UI on loopback for platform proxy access.

Terra targets publish as `edge-terra` and `edge-terra-smoke`. The `edge-terra-smoke` flavor bundles the tiny verified GGUF smoke artifact so a Terra workspace can run the same local-model, OpenHands, CLI, notebook, web UI, audit, and reviewer-packet smoke path without public runtime network access. These tags are `linux/amd64` only until the selected Terra base image supports another architecture, and the Dockerfile pins the Terra base and copied runtime binaries to that declared platform.

Pull-request CI uses `images/platform/terra-ci-base.Dockerfile` plus the `terra-smoke-ci` Bake target to run the same Heartwood platform Dockerfile and smoke scripts without downloading the multi-gigabyte Terra notebook base on every pull request. The main-branch publish workflow builds `terra-runtime` and `terra-smoke` from the real Terra base after freeing runner disk space, then verifies the published Terra tags.

Use `images/platforms.toml` as the source of truth for platform base image, home directory, user, Jupyter prefix, supported architectures, tag names, and required evidence. Do not add a new platform image target without adding a manifest entry, Bake target, static tests, and documentation for the platform-specific validation evidence. The contributor-facing mechanism is documented in `docs/platform-images.md`. Published targets keep SBOM/provenance attestations; local-only CI `--load` targets disable attestations because Docker's local exporter cannot load attested image indexes.
