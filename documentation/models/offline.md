<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Work Without Internet Access

An offline Heartwood workflow requires the executable or container image, a compatible Heartwood-managed model, and all project inputs to be present before network access is removed.
The standard images contain inference software but no model weights.

## Prepare the Project

In an approved connected staging environment:

1. install or pull the exact Heartwood release;
2. create the dedicated project directory;
3. inspect and download a supported model, or import an approved transferred artifact;
4. verify the selected model with `heartwood doctor`; and
5. transfer the complete project through the institution's approved process when the offline host differs.

Model provenance remains in `.heartwood/models/`.
Do not transfer provider tokens or unrelated project state.

## Start a Container Without Networking

From the prepared host project:

```bash
docker run --rm -it \
  --network none \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.7 \
  heartwood
```

The mounted project appears as `/workspace` inside the container, but the host folder remains the durable location.
With networking disabled, only a selected Heartwood-managed model can satisfy inference.

Docker's `none` network isolates the container from the host as well as the internet, so this strict recipe supports the terminal interface only.
To use the browser in an air-gapped deployment, the surrounding platform must provide an authenticated or loopback-only inbound route while independently denying outbound traffic; validate that network design before using it with project data.

## Verify the Boundary

Check that:

- the active model route is loopback;
- no hosted credential is configured;
- the platform blocks outbound traffic independently of Heartwood policy;
- model files and project storage meet the environment's handling requirements; and
- audit exports remain in the approved environment.

Heartwood's no-network integration tests exercise the complete gateway, OpenHands, tool, action, replay, and audit path with deterministic and resource-qualified in-environment inference fixtures.
Deployment isolation remains an infrastructure responsibility.
