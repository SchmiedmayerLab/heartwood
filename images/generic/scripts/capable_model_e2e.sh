#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
project="${HEARTWOOD_CAPABLE_PROJECT:-/tmp/heartwood-capable-project}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="$(cd "${script_dir}/../../.." && pwd)"
runtime_log="${HEARTWOOD_RUNTIME_LOG:-${project}/llama-server.log}"
runtime_port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"

if [[ ! -f "${model_path}" ]]; then
  echo "capable-model artifact is unavailable: ${model_path}" >&2
  exit 66
fi
mkdir -p "${project}"
rm -f "${runtime_log}"

export HEARTWOOD_LOCAL_RUNTIME_PROFILE="llama-cpp-cpu"
export HEARTWOOD_LOCAL_MODEL_CONTEXT="${HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768}"
export HEARTWOOD_LOCAL_MODEL_THREADS="${HEARTWOOD_LOCAL_MODEL_THREADS:-8}"
export HEARTWOOD_LOCAL_RUNTIME_PORT="${runtime_port}"
export HEARTWOOD_RUNTIME_ROOT="${runtime_root}"

bash "${runtime_root}/images/generic/scripts/start_local_runtime.sh" >"${runtime_log}" 2>&1 &
runtime_pid="$!"

cleanup() {
  status="$?"
  trap - EXIT
  kill "${runtime_pid}" >/dev/null 2>&1 || true
  wait "${runtime_pid}" >/dev/null 2>&1 || true
  if ((status != 0)) && [[ -f "${runtime_log}" ]]; then
    echo "llama.cpp runtime log (last 200 lines):" >&2
    tail -n 200 "${runtime_log}" >&2
  fi
  exit "${status}"
}
trap cleanup EXIT

python - <<'PY'
import os
import time
import urllib.error
import urllib.request

port = int(os.environ["HEARTWOOD_LOCAL_RUNTIME_PORT"])
deadline = time.time() + 180
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError) as error:
        last_error = error
        time.sleep(0.2)
else:
    raise SystemExit(f"mounted capable-model runtime did not become ready: {last_error}")
PY

bash "${script_dir}/coding_agent_e2e.sh"
