#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

workspace="${HEARTWOOD_WORKSPACE:-/home/heartwood/.local/share/heartwood/sessions}"
runtime_host="${HEARTWOOD_LOCAL_RUNTIME_HOST:-127.0.0.1}"
runtime_port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"

export HEARTWOOD_WORKSPACE="${workspace}"
export HEARTWOOD_WEB_HOST="${HEARTWOOD_DEMO_WEB_HOST:-0.0.0.0}"
export HEARTWOOD_AGENT_BACKEND="${HEARTWOOD_AGENT_BACKEND:-auto}"

mkdir -p "${workspace}"

model_pid=""
cleanup() {
  if [[ -n "${model_pid}" ]]; then
    kill "${model_pid}" >/dev/null 2>&1 || true
    wait "${model_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "${HEARTWOOD_DEMO_START_LOCAL_RUNTIME:-0}" == "1" ]]; then
  HEARTWOOD_LOCAL_RUNTIME_HOST="${runtime_host}" \
  HEARTWOOD_LOCAL_RUNTIME_PORT="${runtime_port}" \
    bash images/generic/scripts/start_local_runtime.sh &
  model_pid="$!"
  RUNTIME_HOST="${runtime_host}" RUNTIME_PORT="${runtime_port}" python - <<'PY'
import os
import socket
import time

host = os.environ["RUNTIME_HOST"]
port = int(os.environ["RUNTIME_PORT"])
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.1)
raise SystemExit("local runtime did not become ready")
PY
fi

exec bash images/generic/scripts/start_web_ui.sh
