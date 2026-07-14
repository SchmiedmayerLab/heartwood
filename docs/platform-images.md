<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Extend Heartwood to a Platform Image

Platform images are thin additions to an existing research-platform base. They must preserve the platform runtime and add the same Heartwood payload, model-profile contract, repository-verified Skills, and no-weight policy as the generic image.

This guide documents the shared extension mechanism for a reviewed platform integration. An entry in a design document or manifest does not make a platform supported; current evidence is recorded in [Platform Support](platform-support.md).

## Source of Truth

- `images/platforms.toml` declares the base image, architecture, user, home, workdir, entrypoint, ports, Jupyter prefix, proxy behavior, environment contract, registry media types, public tags, model storage, and required evidence.
- `images/platform/Dockerfile` contains the shared additive build.
- `docker-bake.hcl` contains one public runtime target and one local CI target per implemented platform.
- `images/platform/scripts/verify_registry_manifest.py` validates public manifest and image configuration contracts.
- `.github/workflows/container-smoke.yml` builds the platform path and runs local contract tests.
- `.github/workflows/container-image.yml` publishes and verifies the real platform-derived image.

## Add or Adapt a Platform Image

1. Pin a platform-maintained base image and document its source, platform support, update cadence, user, home, workdir, entrypoint, service ports, Jupyter paths, and proxy routes.
2. Add an `images/platforms.toml` entry. Do not encode platform assumptions only in a Dockerfile or workflow.
3. Reuse `images/platform/Dockerfile` when the platform can express its differences through build arguments. Add another Dockerfile only when the base requires materially different installation mechanics.
4. Keep the Heartwood payload list aligned with the generic image: lockfile, packages, fixtures, Skills, evaluations, image scripts, built web UI, README, documentation, and design record.
5. Keep model weights and credentials out of every layer. Make the user's project directory durable, including its `.heartwood/models/` path, and define platform-owned credential bindings.
6. Preserve the base entrypoint and service runtime. Register Heartwood as a separate kernel or tool environment instead of replacing the platform Python.
7. Add a public Bake target and a local CI target. Keep `--set <target>.platform=<architecture>` on `--load` commands and use the Docker driver when a locally tagged CI base is required.
8. Declare the registry manifest media type, config media type, supported platforms, and non-platform manifest policy. Do not assume every control plane accepts an Open Container Initiative index.
9. Extend the registry verifier and tests when the new platform uses a different manifest or authentication contract.
10. Run the required synthetic live evidence pass before calling the platform supported.

## Shared Application Contract

Platform images must expose:

- `heartwood` and the Heartwood Python executable;
- the packaged web UI and notebook bridge;
- the OpenHands SDK and coding tools behind the Heartwood backend adapter;
- the same non-secret model settings, recommendation catalog, and arbitrary Hugging Face planning contract;
- verified bundled Skills loaded through the OpenHands native loader;
- route policy, event persistence, action confirmation, replay, and scrubbed audit export;
- a durable current-directory project whose `.heartwood/` directory contains sessions, settings, OpenHands state, and optional model artifacts.

Do not add a platform-specific agent loop, provider client, model settings format, web contract, or Skill loader. Platform-specific behavior belongs in base-image preservation, identity, route policy, storage, proxy, and deployment adapters.

The platform home and image workdir are starting locations, not Heartwood workspace settings. A user selects a project by starting the CLI, browser server, or notebook kernel from that directory. Platform images must not create or redirect Heartwood to a specially named project directory.

## Continuous Integration

The pull-request platform target must test:

- Dockerfile checks, including secret-in-argument warnings;
- no model weight build arguments or download layers;
- image user, workdir, entrypoint, exposed ports, and required environment;
- platform Python and Jupyter precedence;
- Heartwood kernel registration;
- a writable project and complete private `.heartwood/` layout that survive container replacement;
- inherited and control-plane-specific Jupyter launch routes;
- the packaged Heartwood UI and project-readiness API through the real Jupyter Server Proxy route;
- packaged web assets and notebook API;
- model-profile validation and a real OpenHands conversation against the no-network loopback fixture;
- repository-verified Skill loading and scrubbed audit export.

Local-only CI load targets should not include attestations because Docker's local image exporter does not load manifest lists or attested image indexes. Public generic targets retain attestations; platform targets follow the declared control-plane compatibility contract.

## Registry Verification

Run:

```bash
python3 images/platform/scripts/verify_registry_manifest.py \
  --manifest images/platforms.toml \
  --platform <platform-id> \
  --image-name <registry/repository> \
  --git-sha <published-git-sha>
```

The verifier uses an empty authentication state when called from publication CI, follows a Bearer challenge for anonymous registry access, sends the declared Accept header, and validates manifest media type, config media type, platform set, non-platform manifest policy, user, workdir, entrypoint, ports, and required environment.

Publication CI may pass `--reference <tag-or-sha256-digest>` to validate one staged candidate or immutable tag before the moving platform tag exists. Platform workflows must run their full image and launch smoke suite against the staged digest, create and verify the immutable commit tag, and update the moving tag only as the final promotion step.

Terra tags must return `application/vnd.docker.distribution.manifest.v2+json`. Leonardo rejects an Open Container Initiative index even when `docker manifest inspect` can read it.

## Platform Promotion Gate

Use synthetic data only. Record the image and base digests, platform shape, persistent storage, startup and resume timing, service route, proxy behavior, kernel visibility, model-profile validation, credential-reference mechanism without values, optional model digest, one coding-agent conversation and action decision, CLI and notebook replay counts, scrubbed audit export, runtime network posture, and platform identity binding.

Do not promote a platform to supported based only on local CI. Platform control-plane behavior, identity injection, proxy rewriting, persistent storage, and autopause/resume require a real workspace validation.
