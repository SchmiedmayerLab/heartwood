#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

environment_root=""
model_root=""
state_root=""
model_id="heartwood-carina-demo"
while (($#)); do
  case "$1" in
    --environment-root) environment_root="${2:?missing environment root}"; shift 2 ;;
    --model-root) model_root="${2:?missing model root}"; shift 2 ;;
    --state-root) state_root="${2:?missing state root}"; shift 2 ;;
    --model-id) model_id="${2:?missing model id}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
: "${environment_root:?--environment-root is required}"
: "${model_root:?--model-root is required}"
: "${state_root:?--state-root is required}"
: "${SLURM_JOB_ID:?launch Heartwood inside a Slurm compute allocation}"
: "${LOCAL_SCRATCH_JOB:?Carina job-local scratch is unavailable}"

if [[ ! -x "${environment_root}/heartwood/bin/heartwood" ]]; then
  echo "Heartwood environment is unavailable; run deploy/carina/bootstrap.sh first" >&2
  exit 69
fi
if [[ ! -x "${environment_root}/vllm/bin/vllm" ]]; then
  echo "vLLM environment is unavailable; run deploy/carina/bootstrap.sh first" >&2
  exit 69
fi
"${environment_root}/heartwood/bin/python" "${script_dir}/verify_model_snapshot.py" "${model_root}"

mkdir -p "${state_root}"
staged_model="$(mktemp -d "${LOCAL_SCRATCH_JOB%/}/heartwood-model.XXXXXX")"
runtime_pid=""
cleanup() {
  if [[ -n "${runtime_pid}" ]]; then
    kill "${runtime_pid}" >/dev/null 2>&1 || true
    for _ in {1..10}; do
      if ! kill -0 "${runtime_pid}" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    if kill -0 "${runtime_pid}" >/dev/null 2>&1; then
      kill -KILL "${runtime_pid}" >/dev/null 2>&1 || true
    fi
    wait "${runtime_pid}" >/dev/null 2>&1 || true
  fi
  rm -rf "${staged_model}"
}
trap cleanup EXIT INT TERM
cp -a "${model_root}/." "${staged_model}/"

unset GH_TOKEN GITHUB_TOKEN HF_TOKEN HUGGING_FACE_HUB_TOKEN
unset OPENAI_API_KEY ANTHROPIC_API_KEY AZURE_API_KEY
export HEARTWOOD_PLATFORM=carina
export HEARTWOOD_AGENT_BACKEND=openhands-sdk
export HEARTWOOD_LOCAL_MODEL_PATH="${staged_model}"
export HEARTWOOD_LOCAL_MODEL_ALIAS="${model_id}"
export HEARTWOOD_VLLM_EXECUTABLE="${environment_root}/vllm/bin/vllm"
export PATH="${environment_root}/heartwood/bin:${PATH}"

bash "${repo_root}/images/gpu/start_vllm.sh" >"${state_root}/vllm-${SLURM_JOB_ID}.log" 2>&1 &
runtime_pid="$!"

python - <<'PY'
import json
import time
import urllib.request

deadline = time.time() + 300
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/v1/models", timeout=2) as response:
            if response.status == 200 and json.load(response).get("data"):
                break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit("vLLM did not become ready within 300 seconds")
PY

workspace="${state_root}/sessions"
if [[ ! -f "${state_root}/setup.json" ]]; then
  heartwood --workspace "${workspace}" setup \
    --model-source local --model-id "${model_id}" --non-interactive --yes
fi
heartwood --workspace "${workspace}" --session-id carina-demo chat
