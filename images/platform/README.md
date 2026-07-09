<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform-Derived Heartwood Images

The platform image layer builds Heartwood inside notebook images that must inherit behavior from a controlled research platform. The current implemented platform target is Terra. Future Seven Bridges and DNAnexus targets should use the same Dockerfile, manifest, and Bake structure after their base images and proxy behavior are validated.

The platform Dockerfile keeps the platform base image as the final stage, installs a managed Python 3.12 Heartwood environment under `/opt/heartwood/.venv`, leaves the platform Jupyter environment ahead of Heartwood on `PATH`, restores the platform home as the final working directory, registers a Heartwood Jupyter kernel when the platform exposes a Jupyter prefix, preserves `/opt/heartwood/docs/terra-jupyter-demo.ipynb`, and serves the Heartwood web UI on loopback for platform proxy access.

Terra targets publish as `edge-terra`, model-explicit alias `edge-terra-coder-7b`, and `edge-terra-smoke`. The `edge-terra` image bundles Qwen2.5-Coder-7B-Instruct Q4_K_M so a Terra workspace can run a useful local-model, OpenHands, CLI, notebook, web UI, audit, and reviewer-packet demo without mounting weights. The `edge-terra-smoke` flavor bundles the tiny verified GGUF smoke artifact for CI and diagnostics. These tags are `linux/amd64` Docker schema-2 image manifests only until the selected Terra base image supports another architecture and Leonardo accepts the corresponding manifest shape, and the Dockerfile pins the Terra base and copied runtime binaries to that declared platform.

Pull-request CI uses `images/platform/terra-ci-base.Dockerfile` plus the `terra-smoke-ci` Bake target to run the same Heartwood platform Dockerfile and smoke scripts without downloading the multi-gigabyte Terra notebook base or 7B model on every pull request. The CI base installs real Jupyter notebook packages with Terra-style `/etc/jupyter` configuration, and the CI image verifies that platform Python/Jupyter remain ahead of the Heartwood virtual environment, the Heartwood kernel is registered, the inherited entrypoint serves `/notebooks/`, and the Leonardo-style `run-jupyter.sh` launch path serves `/notebooks/<project>/<cluster>/`. The main-branch publish workflow builds `terra-runtime` and `terra-smoke` from the real Terra base after freeing runner disk space, verifies the published Terra tags and launch-critical image config, then pulls the published Terra runtime image and repeats the Jupyter contract and launch smokes.

Use `images/platforms.toml` as the source of truth for platform base image, home directory, user, Jupyter prefix, entrypoint, exposed ports, supported architectures, tag names, manifest media type, config media type, attestation policy, non-platform manifest policy, and required evidence. Do not add a new platform image target without adding a manifest entry, Bake target, static tests, registry verification through `images/platform/scripts/verify_registry_manifest.py`, and documentation for the platform-specific validation evidence. The contributor-facing mechanism is documented in `docs/platform-images.md`. Generic published targets keep cache, SBOM, and provenance settings; Terra-facing published targets disable Buildx attestations and force Docker schema-2 media types for Leonardo compatibility. Local-only CI `--load` targets use the Docker driver when they depend on a locally tagged base image and disable attestations because Docker's local exporter cannot load attested image indexes.
