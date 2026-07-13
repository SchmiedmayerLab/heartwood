#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

vllm_python="${HEARTWOOD_VLLM_PYTHON:-/opt/heartwood-vllm/bin/python}"

if [[ ! -x "${vllm_python}" ]]; then
  echo "vLLM Python is unavailable: ${vllm_python}" >&2
  exit 69
fi

"${vllm_python}" -c \
  'import torchcodec, vllm; from importlib.metadata import version; print(version("torchcodec"), version("vllm"))'

model_artifact="$(
  find /opt /home -type f -size +10M \
    \( -name '*.gguf' -o -name '*.safetensors' -o -name '*.bin' \) \
    -print -quit
)"
if [[ -n "${model_artifact}" ]]; then
  echo "GPU runtime image contains a model artifact: ${model_artifact}" >&2
  exit 65
fi

echo "GPU runtime verification passed"
