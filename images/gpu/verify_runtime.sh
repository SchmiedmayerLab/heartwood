#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

find_model_artifact() {
  local allowed_runtime_asset
  local candidate
  allowed_runtime_asset="${HEARTWOOD_VLLM_ROOT:-/opt/heartwood-vllm}/lib/python3.12/site-packages/compressed_tensors/transform/utils/hadamards.safetensors"

  while IFS= read -r -d '' candidate; do
    if [[ "${candidate}" == "${allowed_runtime_asset}" ]]; then
      continue
    fi
    printf '%s\n' "${candidate}"
    return 0
  done < <(
    find "$@" -type f \
      \( -name '*.gguf' -o -name '*.safetensors' -o \( -name '*.bin' -size +10M \) \) \
      -print0
  )
}

verify_no_model_artifacts() {
  local model_artifact
  model_artifact="$(find_model_artifact "$@")"
  if [[ -n "${model_artifact}" ]]; then
    echo "GPU runtime image contains a model artifact: ${model_artifact}" >&2
    return 65
  fi
  return 0
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

vllm_python="${HEARTWOOD_VLLM_PYTHON:-/opt/heartwood-vllm/bin/python}"
if [[ ! -x "${vllm_python}" ]]; then
  echo "vLLM Python is unavailable: ${vllm_python}" >&2
  exit 69
fi

"${vllm_python}" -c \
  'import torchcodec, vllm; from importlib.metadata import version; print(version("torchcodec"), version("vllm"))'

verify_no_model_artifacts /opt /home

echo "GPU runtime verification passed"
