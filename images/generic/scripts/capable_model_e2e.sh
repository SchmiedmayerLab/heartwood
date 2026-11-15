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
runtime_artifact_id="${HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID:-}"
if [[ -z "${runtime_artifact_id}" ]]; then
  runtime_artifact_id="$(basename -- "$(dirname -- "${model_path}")")"
fi
if [[ -z "${runtime_artifact_id}" || "${runtime_artifact_id}" == "." || \
  "${runtime_artifact_id}" == ".." || "${runtime_artifact_id}" == */* ]]; then
  echo "capable-model runtime artifact id is invalid for: ${model_path}" >&2
  exit 64
fi

if [[ ! -f "${model_path}" ]]; then
  echo "capable-model artifact is unavailable: ${model_path}" >&2
  exit 66
fi
mkdir -p "${project}"
rm -f "${runtime_log}"

export HEARTWOOD_LOCAL_RUNTIME_PROFILE="llama-cpp-cpu"
export HEARTWOOD_LOCAL_MODEL_CONTEXT="${HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768}"
export HEARTWOOD_LOCAL_MODEL_THREADS="${HEARTWOOD_LOCAL_MODEL_THREADS:-8}"
export HEARTWOOD_RUNTIME_ROOT="${runtime_root}"
export HEARTWOOD_LOCAL_RUNTIME_ARTIFACT_ID="${runtime_artifact_id}"

exec "${HEARTWOOD_PYTHON:-python}" "${script_dir}/qualify_runtime.py" \
  --project "${project}" --log "${runtime_log}" \
  --runtime "${script_dir}/start_local_runtime.sh" -- \
  bash "${script_dir}/coding_agent_e2e.sh"
