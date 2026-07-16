#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

image_reference="${1:?usage: bind_mount_user_smoke.sh <image-reference> <platform>}"
docker_platform="${2:?usage: bind_mount_user_smoke.sh <image-reference> <platform>}"
project_root="$(mktemp -d)"

cleanup() {
  rm -rf "${project_root}"
}
trap cleanup EXIT

run_heartwood() {
  docker run --rm --platform "${docker_platform}" --network none \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp \
    --volume "${project_root}:/workspace" \
    "${image_reference}" "$@"
}

run_heartwood heartwood models add bind-mount-smoke \
  --model openai/bind-mount-smoke \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select
run_heartwood heartwood models validate bind-mount-smoke

test -d "${project_root}/.heartwood"
printf '%s\n' host-writable > "${project_root}/.heartwood/bind-mount-probe"
test "$(cat "${project_root}/.heartwood/bind-mount-probe")" = "host-writable"

echo "Host-user bind-mount smoke: ok"
