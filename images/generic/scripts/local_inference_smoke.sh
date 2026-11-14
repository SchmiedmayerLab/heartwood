#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
heartwood_python="${HEARTWOOD_PYTHON:-python}"
project="$(mktemp -d "${TMPDIR:-/tmp}/heartwood-llama-smoke.XXXXXX")"
log_file="${project}/runtime.log"
cleanup() {
  status="$?"
  if [ "${status}" -ne 0 ]; then
    tail -n 200 "${log_file}" >&2 || true
  fi
  rm -f "${log_file}"
  rmdir "${project}"
  return "${status}"
}
trap cleanup EXIT

export HEARTWOOD_LOCAL_MODEL_CONTEXT=256
export HEARTWOOD_LOCAL_MODEL_THREADS=2
"${heartwood_python}" "${script_dir}/qualify_runtime.py" \
  --project "${project}" --log "${log_file}" \
  --runtime "${script_dir}/start_local_runtime.sh" -- "${heartwood_python}" - <<'PYTHON'
import os
from heartwood.gateway._managed_inference import managed_inference_client

with managed_inference_client(os.environ, timeout=60) as client:
    response = client.post("chat/completions", json={
        "model": os.environ.get("HEARTWOOD_MANAGED_MODEL_ALIAS", "heartwood-managed-runtime"),
        "messages": [{"role": "user", "content": "Once upon a time"}],
        "max_tokens": 4,
        "temperature": 0,
    })
    response.raise_for_status()
    choices = response.json().get("choices")
    if not isinstance(choices, list) or not choices:
        raise SystemExit("llama-server returned no completion choices")
    slots = client.get("http://127.0.0.1:8765/slots")
    if slots.status_code != 501 or slots.json().get("error", {}).get("type") != "not_supported_error":
        raise SystemExit("llama-server slot inspection must be disabled")
print("Mounted Heartwood-managed inference smoke: ok")
PYTHON
