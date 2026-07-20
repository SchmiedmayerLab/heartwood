#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

image_ref="${1:?image reference is required}"
docker_platform="${HEARTWOOD_TERRA_DOCKER_PLATFORM:-linux/amd64}"
project_root="/home/jupyter/synthetic-analysis"
state_volume="heartwood-terra-project-smoke-$$"

cleanup() {
  docker volume rm --force "${state_volume}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "${state_volume}" >/dev/null

docker run --rm --platform "${docker_platform}" --network none \
  --volume "${state_volume}:/home/jupyter" \
  --env GOOGLE_PROJECT=heartwood-ci \
  --env WORKSPACE_ID=terra-project-smoke \
  --env "HEARTWOOD_TEST_PROJECT=${project_root}" \
  --entrypoint bash \
  "${image_ref}" -c '
    set -euo pipefail
    mkdir -p "${HEARTWOOD_TEST_PROJECT}"
    cd "${HEARTWOOD_TEST_PROJECT}"
    /opt/heartwood/images/platform/scripts/terra_image_smoke.sh
    heartwood doctor --json >/tmp/heartwood-readiness.json
    grep --fixed-strings "${HEARTWOOD_TEST_PROJECT}" /tmp/heartwood-readiness.json
    heartwood --session-id terra-project-persistence pause \
      | grep --fixed-strings "Session paused"
    printf "%s\n" persisted > .heartwood/cache/terra-project-persistence
    test ! -e /home/jupyter/.heartwood
  '

docker run --rm --platform "${docker_platform}" --network none \
  --volume "${state_volume}:/home/jupyter" \
  --env GOOGLE_PROJECT=heartwood-ci \
  --env WORKSPACE_ID=terra-project-smoke \
  --env "HEARTWOOD_TEST_PROJECT=${project_root}" \
  --workdir "${project_root}" \
  --entrypoint bash \
  "${image_ref}" -c '
    set -euo pipefail
    test "$(cat .heartwood/cache/terra-project-persistence)" = persisted
    /opt/heartwood/images/platform/scripts/terra_image_smoke.sh
    heartwood --session-id terra-project-persistence replay \
      | grep --fixed-strings "Session paused"
    test ! -e /home/jupyter/.heartwood
  '

echo "Terra current-directory project persistence smoke: ok"
