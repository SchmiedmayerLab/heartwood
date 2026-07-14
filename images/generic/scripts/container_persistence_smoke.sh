#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

compose_file="${1:-images/generic/compose.yaml}"
state_volume="heartwood-state-smoke-$$"

cleanup() {
  docker volume rm --force "${state_volume}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "${state_volume}" >/dev/null

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${state_volume}:/workspace" \
  heartwood heartwood models add local-persistence \
  --model openai/local-persistence \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${state_volume}:/workspace" \
  heartwood heartwood models validate local-persistence

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${state_volume}:/workspace" \
  heartwood bash -c 'printf %s persisted > /workspace/.heartwood/models/persistence-probe'

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${state_volume}:/workspace" \
  heartwood bash -c 'test "$(cat /workspace/.heartwood/models/persistence-probe)" = persisted'

echo "Named-volume persistence smoke: ok"
