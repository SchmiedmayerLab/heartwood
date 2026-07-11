<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Platform-Derived Heartwood Runtime

This directory defines the implemented platform-image mechanism and Terra target. Current platform and validation status is recorded in [Platform Support](../../docs/platform-support.md); future platform work is recorded in the [Delivery Roadmap](../../design/09-implementation-plan.md).

The platform Dockerfile adds the same Heartwood application payload as the generic image to a controlled platform base while preserving the platform user, home, Jupyter runtime, entrypoint, service routes, and proxy behavior. The implemented target is Terra.

The public Terra tags are `edge-terra` and `sha-<git-sha>-terra`. They contain no model weights or credentials. Optional local artifacts belong in `/home/jupyter/heartwood-workspace/models`; hosted or managed model services are configured through the same non-secret profile contract as the generic image.

Terra tags are `linux/amd64` Docker schema-2 manifests because the selected Terra base is AMD64-only and Leonardo does not accept an Open Container Initiative index during image auto-detection. Main publication stages one untagged digest, validates its registry and runtime configuration, runs the Jupyter contract, inherited entrypoint, Leonardo route, OpenHands, and mounted local-inference smokes against that digest, creates and verifies the immutable commit tag, and moves `edge-terra` last.

Pull requests use `images/platform/terra-ci-base.Dockerfile` only as a lightweight surrogate for the large upstream Terra base. Heartwood itself is still built through `images/platform/Dockerfile`, and the surrogate provides real Jupyter packages and Terra-style launch configuration. `images/platforms.toml` is the source of truth for platform contracts and evidence.

The pinned `terra-jupyter-python:1.1.6` base remains listed by Terra. Terra's newer slim `terra-base:1.0.0` is a future migration candidate, not a drop-in documentation change; adopting it requires the complete image, Jupyter, Leonardo, proxy, storage, and publication contract to pass again.
