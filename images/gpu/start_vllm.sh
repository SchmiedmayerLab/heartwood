#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
host="${HEARTWOOD_LOCAL_RUNTIME_HOST:-127.0.0.1}"
port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
alias="${HEARTWOOD_LOCAL_MODEL_ALIAS:-heartwood-local-runtime}"
tool_parser="${HEARTWOOD_VLLM_TOOL_PARSER:-hermes}"
context="${HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768}"
vllm="${HEARTWOOD_VLLM_EXECUTABLE:-/opt/heartwood-vllm/bin/heartwood-vllm}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

if [[ "${host}" != "127.0.0.1" && "${host}" != "localhost" && "${host}" != "::1" ]]; then
  echo "vLLM must bind to loopback, got ${host}" >&2
  exit 64
fi
if [[ ! -e "${model_path}" ]]; then
  echo "model path does not exist: ${model_path}" >&2
  exit 66
fi
if [[ ! -x "${vllm}" ]]; then
  echo "vLLM executable is unavailable: ${vllm}" >&2
  exit 69
fi

exec "${vllm}" serve "${model_path}" \
  --host "${host}" \
  --port "${port}" \
  --served-model-name "${alias}" \
  --max-model-len "${context}" \
  --enable-auto-tool-choice \
  --tool-call-parser "${tool_parser}"
