#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
log_file="$(mktemp "${TMPDIR:-/tmp}/heartwood-llama-smoke.XXXXXX.log")"

HEARTWOOD_LOCAL_MODEL_PATH="${model_path}" \
HEARTWOOD_LOCAL_MODEL_CONTEXT=256 \
HEARTWOOD_LOCAL_MODEL_THREADS=2 \
HEARTWOOD_LOCAL_RUNTIME_PORT="${port}" \
  bash "${script_dir}/start_local_runtime.sh" >"${log_file}" 2>&1 &
runtime_pid="$!"

cleanup() {
  status="$?"
  kill "${runtime_pid}" >/dev/null 2>&1 || true
  wait "${runtime_pid}" >/dev/null 2>&1 || true
  if [ "${status}" -ne 0 ]; then
    printf 'Heartwood-managed inference runtime log:\n' >&2
    tail -n 200 "${log_file}" >&2 || true
  fi
  rm -f "${log_file}"
  return "${status}"
}
trap cleanup EXIT

HEARTWOOD_LOCAL_RUNTIME_PORT="${port}" python - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request

port = int(os.environ["HEARTWOOD_LOCAL_RUNTIME_PORT"])
deadline = time.time() + 180
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError) as error:
        last_error = error
        time.sleep(0.2)
else:
    raise SystemExit(f"llama-server did not become ready: {last_error}")

payload = json.dumps(
    {
        "model": "heartwood-managed-runtime",
        "messages": [{"role": "user", "content": "Once upon a time"}],
        "max_tokens": 4,
        "temperature": 0,
    }
).encode()
request = urllib.request.Request(
    f"http://127.0.0.1:{port}/v1/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=60) as response:
    result = json.load(response)
choices = result.get("choices")
if not isinstance(choices, list) or not choices:
    raise SystemExit(f"llama-server returned no completion choices: {result}")
print("Mounted Heartwood-managed inference smoke: ok")
PY
