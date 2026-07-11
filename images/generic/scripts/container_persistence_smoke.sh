#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

compose_file="${1:-images/generic/compose.yaml}"
state_volume="heartwood-state-smoke-$$"
model_volume="heartwood-model-smoke-$$"
workspace="/home/heartwood/.local/share/heartwood/sessions"
model_cache="/home/heartwood/.cache/heartwood/models"

cleanup() {
  docker volume rm --force "${state_volume}" "${model_volume}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "${state_volume}" >/dev/null
docker volume create "${model_volume}" >/dev/null

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${state_volume}:/home/heartwood/.local/share/heartwood" \
  heartwood heartwood --workspace "${workspace}" models add local-persistence \
  --model openai/local-persistence \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${state_volume}:/home/heartwood/.local/share/heartwood" \
  heartwood heartwood --workspace "${workspace}" models validate local-persistence

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${model_volume}:${model_cache}" \
  heartwood bash -c 'printf %s persisted > /home/heartwood/.cache/heartwood/models/persistence-probe'

docker compose -f "${compose_file}" run --rm --no-deps \
  --volume "${model_volume}:${model_cache}" \
  heartwood bash -c 'test "$(cat /home/heartwood/.cache/heartwood/models/persistence-probe)" = persisted'

echo "Named-volume persistence smoke: ok"
