<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform Image Extension Guide

Heartwood platform images are derived notebook images for controlled research environments that require a platform-specific base image, home directory, Jupyter prefix, proxy route, registry policy, and launch evidence. The current implemented target is Terra; future Seven Bridges, DNAnexus, or site-specific notebook images must follow the same mechanism instead of creating a parallel Docker path.

## Extension Contract

Every platform image is defined by five repository surfaces:

| Surface | Purpose |
|---|---|
| `images/platforms.toml` | Source of truth for platform name, base image, platform architecture, home directory, user, Jupyter prefix, proxy behavior, tag names, registry policy, and required evidence. |
| `images/platform/Dockerfile` | Shared platform-image Dockerfile that keeps the platform base as the final stage, installs Heartwood under `/opt/heartwood`, registers the optional Jupyter kernel, copies packaged docs and web UI assets, and preserves the platform entrypoint. |
| `docker-bake.hcl` | Build graph for runtime, smoke, CI-smoke, tags, build arguments, supported platforms, SBOM, provenance, and cache behavior. |
| `.github/workflows/container-smoke.yml` and `.github/workflows/container-image.yml` | Pull-request smoke checks and main-branch publication checks for the platform image targets. |
| `packages/compliance/tests/test_container_assets.py` and `packages/compliance/tests/test_documentation_assets.py` | Static guardrails that keep image, workflow, documentation, tag, secret, and live-validation requirements synchronized. |

## Add Or Adapt A Platform Image

1. Add a platform entry to `images/platforms.toml`. Record the exact base image, source repository or vendor page, parent image if known, supported architectures, unsupported architectures, home directory, runtime user, Jupyter prefix, proxy mechanism, registry policy, tags, CI requirement, and live workspace evidence requirement.
2. Add or extend Bake targets in `docker-bake.hcl`. Use `_platform_common` for shared behavior and a platform-specific common target for base image, base platform, runtime architecture, platform home, user, and Jupyter prefix. Keep runtime images without bundled model artifacts and smoke images with only the tiny verified smoke artifact unless a larger model artifact has completed provenance and license review.
3. Add CI coverage in `.github/workflows/container-smoke.yml`. Pull-request CI should build a lightweight platform-compatible CI base when the real base is too large or slow for every pull request, then build the smoke target through the shared platform Dockerfile, run `images/platform/scripts/terra_image_smoke.sh` or the platform-specific equivalent, and run `images/generic/scripts/offline_stack_smoke.sh` with runtime network disabled. Local `--load` smoke targets must disable attestations because Docker's local exporter cannot load SBOM/provenance image indexes; main-branch published targets must keep SBOM and provenance attestations.
4. Add main-branch publication in `.github/workflows/container-image.yml`. Publish only architectures supported by the selected platform base. Verify each public tag after publication with `docker buildx imagetools inspect`; generic public tags must expose both `linux/amd64` and `linux/arm64`, while platform tags must expose only the architectures listed in `images/platforms.toml`.
5. Update documentation. Link the platform from `docs/container-images.md`, add or update the platform runbook, and keep `design/09-implementation-plan.md` current with implemented requirements, exclusions, live-validation evidence, and future work.
6. Add static tests. Tests must assert the base image, supported architecture set, tag names, no baked secrets, Jupyter kernel registration, packaged docs, packaged web UI, CI smoke commands, main-branch publish commands, and live workspace evidence requirements.
7. Run local verification before opening or updating a pull request:

```bash
docker buildx build --check --platform linux/amd64 --file images/platform/Dockerfile .
docker buildx bake --file docker-bake.hcl --print terra-runtime terra-smoke terra-smoke-ci
docker buildx bake --file docker-bake.hcl --load --set terra-smoke-ci.platform=linux/amd64 terra-smoke-ci
docker run --rm --platform linux/amd64 --network none --entrypoint bash ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke-ci images/platform/scripts/terra_image_smoke.sh
docker run --rm --platform linux/amd64 --network none --entrypoint bash ghcr.io/schmiedmayerlab/heartwood:edge-terra-smoke-ci images/generic/scripts/offline_stack_smoke.sh
```

Replace the target and tag names when adding a new platform. Keep `--set <target>.platform=<architecture>` on any `--load` Bake command used by a Docker container-driver builder, and keep local-only CI load targets without attestations, because Docker's local image exporter does not load manifest lists or attested image indexes.

## Platform Variants

Use a new Bake target when the base image, bundled model policy, runtime architecture, or publication tag changes. Use a new `images/platforms.toml` platform entry when the platform home directory, notebook service path, proxy behavior, identity headers, registry policy, or live validation evidence changes. A site-specific Terra base can inherit the Terra platform entry only if it preserves `/home/jupyter`, `/opt/conda`, the Terra notebook entrypoint behavior, and the same proxy shape; otherwise add a separate platform entry and evidence checklist.

## Required Live Evidence

Before documenting a platform image as supported in that live platform, record the custom image digest, selected base image digest, VM shape, disk size, startup time, notebook home behavior, Jupyter kernel visibility, proxy URL shape, one synthetic researcher web UI chat interaction, CLI replay count, notebook API replay count, audit export path, reviewer packet path, runtime network posture, and any identity or proxy headers exposed to Heartwood. Synthetic data only is allowed for this validation until the platform policy and compliance evidence explicitly permit controlled data.
