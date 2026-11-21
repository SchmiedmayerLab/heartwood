#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# Builds the worker, runtime and hosted gateway candidate images from candidate wheels
# and checks that they start. The Docker and the Kubernetes qualification use the worker
# and runtime images; the platform chart runs the hosted gateway image.
# WHEELS holds the directories `first` and `application-first` that
# deploy/build-worker-candidate.py wrote.
# Usage: build-candidate-images.sh WHEELS REVISION [TAG]

set -euo pipefail

wheels="${1:?candidate wheel directory is required}"
revision="${2:?source revision is required}"
tag="${3:-qualification}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

rm -rf "${wheels}/image"
mkdir -p "${wheels}/image"
cp -R "${wheels}/first" "${wheels}/image/sdk"
cp -R "${wheels}/application-first" "${wheels}/image/application"
for target in worker runtime hosted-gateway; do
  docker buildx build --load --file "${root}/images/Dockerfile" --target "${target}-image" \
    --build-arg HEARTWOOD_DEPENDENCY_PROFILE=candidate \
    --build-arg "HEARTWOOD_IMAGE_FLAVOR=${target}" \
    --build-arg "HEARTWOOD_REVISION=${revision}" \
    --build-context "worker-qualification-artifacts=${wheels}/image" \
    --tag "heartwood-${target}:${tag}" "${root}"
done
python3 "${root}/images/worker/smoke.py" "heartwood-worker:${tag}"
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  "heartwood-runtime:${tag}" heartwood --help >/dev/null
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  "heartwood-hosted-gateway:${tag}" heartwood gateway serve --help >/dev/null
