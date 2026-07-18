<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Build a Platform-Specific Image

This maintainer guide defines the extension boundary for a managed research platform. Platform images add the shared Heartwood payload to a platform-owned base; they do not create another agent, model, state, or interface implementation.

## Use the Declarative Contract

- `images/platforms.toml` declares the base image, architecture, user, home, working directory, entrypoint, ports, Jupyter prefix, proxy behavior, registry format, tags, storage, and required checks.
- `images/platform/Dockerfile` implements the additive build.
- `docker-bake.hcl` defines public and local validation targets.
- `images/platform/scripts/verify_registry_manifest.py` verifies registry and image-configuration contracts.

## Add an Integration

1. Pin a platform-maintained base image.
2. Record its user, home, working directory, entrypoint, ports, Jupyter behavior, proxy, storage, architecture, and registry requirements.
3. Add a manifest entry and reuse the shared Dockerfile when build arguments can express the differences.
4. Preserve the base service and register Heartwood as an additional command, kernel, and web application.
5. Keep model weights and credentials out of image layers.
6. Keep the current-directory project and `.heartwood/` state contract unchanged.
7. Add image, startup, persistence, proxy, model-route, action-review, and audit tests.
8. Validate an immutable published artifact in the real platform before documenting it as available.

Platform differences belong in image preservation, storage, identity, routing, and policy adapters. Do not add a platform-specific agent loop, provider client, model settings format, web contract, or Skill loader.

## Preserve the Application Contract

Every platform image must expose the same:

- Heartwood command and Python environment;
- OpenHands-backed session gateway;
- terminal, browser assets, and notebook bridge where routing permits;
- model settings and local-model planner;
- bundled Skills;
- route policy, grouped confirmation, replay, and audit export.

The platform home is a starting location, not a Heartwood workspace. Users choose the project by starting the command, server, or notebook from a dedicated directory.

## Verify Publication

Registry format is part of the platform contract. Terra requires an AMD64 Docker schema-2 manifest because Leonardo rejects the generic multi-platform Open Container Initiative index.

Run the repository's manifest verifier against the staged digest and immutable tag. Promote a moving tag only after image and launch tests pass for the exact descriptor. Use synthetic data for all public checks.
