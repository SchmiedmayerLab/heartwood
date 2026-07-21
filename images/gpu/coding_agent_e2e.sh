#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

configuration_id="${HEARTWOOD_GPU_CONFIGURATION_ID:?HEARTWOOD_GPU_CONFIGURATION_ID is required}"
model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
project="${HEARTWOOD_CAPABLE_PROJECT:-/tmp/heartwood-gpu-qualification}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="${HEARTWOOD_RUNTIME_ROOT:-$(cd "${script_dir}/../.." && pwd)}"
runtime_log="${HEARTWOOD_RUNTIME_LOG:-${project}/vllm.log}"
runtime_metadata="${HEARTWOOD_QUALIFICATION_RUNTIME_METADATA:-${project}/gpu-runtime.json}"
runtime_port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
vllm_executable="${HEARTWOOD_VLLM_EXECUTABLE:-/opt/heartwood-vllm/bin/heartwood-vllm}"
vllm_python="${HEARTWOOD_VLLM_PYTHON:-/opt/heartwood-vllm/bin/python}"

if [[ ! -d "${model_path}" ]]; then
  echo "vLLM model snapshot is unavailable: ${model_path}" >&2
  exit 66
fi
mkdir -p "${project}"
project="$(cd "${project}" && pwd -P)"
model_path="$(cd "${model_path}" && pwd -P)"
rm -f "${runtime_log}" "${runtime_metadata}"

configuration="$(python "${script_dir}/qualification_config.py" "${configuration_id}")"
snapshot_id="$(jq -er '.configuration.model_snapshot' <<<"${configuration}")"
repository="$(jq -er '.configuration.model_repository' <<<"${configuration}")"
revision="$(jq -er '.configuration.model_revision' <<<"${configuration}")"
context="$(jq -er '.configuration.context_window' <<<"${configuration}")"
tensor_parallel="$(jq -er '.configuration.tensor_parallel_size' <<<"${configuration}")"
tool_parser="$(jq -er '.configuration.tool_call_parser' <<<"${configuration}")"
startup_min="$(jq -er '.configuration.startup_seconds_min' <<<"${configuration}")"
startup_max="$(jq -er '.configuration.startup_seconds_max' <<<"${configuration}")"

echo "Verifying the pinned ${repository}@${revision} snapshot..."
python - "${model_path}" "${snapshot_id}" "${repository}" "${revision}" <<'PY'
import json
import sys
from pathlib import Path

from heartwood.gateway import verify_model_snapshot

root = Path(sys.argv[1])
expected = {
    "snapshot_id": sys.argv[2],
    "source_repository": sys.argv[3],
    "source_revision": sys.argv[4],
}
source = json.loads((root / "HEARTWOOD-SOURCE.json").read_text(encoding="utf-8"))
if any(source.get(key) != value for key, value in expected.items()):
    raise SystemExit(f"model snapshot source does not match the qualification configuration: {source}")
verify_model_snapshot(root)
PY

python - "${configuration}" <<'PY'
import json
import os
import sys

from heartwood.gateway import inspect_gpu_environment

payload = json.loads(sys.argv[1])
configuration = payload["configuration"]
environment = inspect_gpu_environment(os.environ.get("HEARTWOOD_PLATFORM", "generic"), os.environ)
compatible, reason = environment.assess(
    gpu_count=configuration["gpu_count"],
    gpu_memory_bytes=configuration["minimum_gpu_memory_bytes"],
)
print(reason)
if not compatible or not environment.visible_devices:
    raise SystemExit("the requested qualification requires compatible GPUs visible in this process")
PY

"${script_dir}/verify_runtime.sh"
"${vllm_executable}" --version >/dev/null
"${vllm_python}" - "${runtime_metadata}" <<'PY'
import json
import subprocess
import sys
from importlib.metadata import version

import torch

query = subprocess.run(
    [
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ],
    check=True,
    capture_output=True,
    text=True,
)
payload = {
    "vllm_version": version("vllm"),
    "pytorch_version": version("torch"),
    "cuda_version": torch.version.cuda,
    "visible_gpus": [line.strip() for line in query.stdout.splitlines() if line.strip()],
}
with open(sys.argv[1], "w", encoding="utf-8") as file:
    json.dump(payload, file, indent=2, sort_keys=True)
    file.write("\n")
PY

export HEARTWOOD_LOCAL_RUNTIME_PROFILE="vllm-cuda"
export HEARTWOOD_LOCAL_MODEL_PATH="${model_path}"
export HEARTWOOD_LOCAL_MODEL_CONTEXT="${context}"
export HEARTWOOD_LOCAL_RUNTIME_PORT="${runtime_port}"
export HEARTWOOD_VLLM_TENSOR_PARALLEL_SIZE="${tensor_parallel}"
export HEARTWOOD_VLLM_TOOL_PARSER="${tool_parser}"
export HEARTWOOD_RUNTIME_ROOT="${runtime_root}"
export HEARTWOOD_QUALIFICATION_MODEL_REPOSITORY="${repository}"
export HEARTWOOD_QUALIFICATION_MODEL_REVISION="${revision}"
export HEARTWOOD_QUALIFICATION_RUNTIME_METADATA="${runtime_metadata}"

echo "Loading ${repository}; expected startup is approximately ${startup_min}-${startup_max} seconds."
bash "${script_dir}/start_vllm.sh" >"${runtime_log}" 2>&1 &
runtime_pid="$!"

cleanup() {
  status="$?"
  trap - EXIT
  kill "${runtime_pid}" >/dev/null 2>&1 || true
  wait "${runtime_pid}" >/dev/null 2>&1 || true
  if ((status != 0)) && [[ -f "${runtime_log}" ]]; then
    echo "vLLM runtime log (last 240 lines):" >&2
    tail -n 240 "${runtime_log}" >&2
  fi
  exit "${status}"
}
trap cleanup EXIT

python - "${runtime_port}" "${startup_max}" <<'PY'
import sys
import time
import urllib.error
import urllib.request

port = int(sys.argv[1])
deadline = time.time() + int(sys.argv[2]) + 120
next_update = time.time() + 30
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            if response.status == 200:
                print("vLLM is ready; starting coding-agent acceptance.")
                break
    except (OSError, urllib.error.URLError) as error:
        last_error = error
    if time.time() >= next_update:
        print("Still loading the model; Heartwood will continue when vLLM is ready.", flush=True)
        next_update = time.time() + 30
    time.sleep(1)
else:
    raise SystemExit(f"vLLM did not become ready before the qualification timeout: {last_error}")
PY

bash "${runtime_root}/images/generic/scripts/coding_agent_e2e.sh"
