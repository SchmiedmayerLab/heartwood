#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

profile="${HEARTWOOD_LOCAL_RUNTIME_PROFILE:-llama-cpp-cpu}"
host="${HEARTWOOD_LOCAL_RUNTIME_HOST:-127.0.0.1}"
port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
request_log="${HEARTWOOD_MODEL_REQUEST_LOG:-/tmp/heartwood-local-model-requests.jsonl}"
model_path="${HEARTWOOD_LOCAL_MODEL_PATH:-}"
model_alias="${HEARTWOOD_LOCAL_MODEL_ALIAS:-heartwood-local-runtime}"
default_threads="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
n_ctx="${HEARTWOOD_LOCAL_MODEL_CONTEXT:-4096}"
n_threads="${HEARTWOOD_LOCAL_MODEL_THREADS:-${default_threads}}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="$(cd "${script_dir}/../../.." && pwd)"

if [[ "${host}" != "127.0.0.1" && "${host}" != "localhost" && "${host}" != "::1" ]]; then
  echo "local runtime must bind to loopback, got ${host}" >&2
  exit 64
fi

case "${profile}" in
  stub-loopback)
    exec python "${runtime_root}/images/generic/scripts/local_model_stub.py" \
      --host "${host}" \
      --port "${port}" \
      --request-log "${request_log}"
    ;;
  llama-cpp-cpu)
    if [[ -z "${model_path}" || ! -f "${model_path}" ]]; then
      echo "llama-cpp-cpu requires HEARTWOOD_LOCAL_MODEL_PATH to reference a mounted or downloaded GGUF file" >&2
      exit 66
    fi
    if ! command -v llama-server >/dev/null 2>&1; then
      echo "local runtime profile llama-cpp-cpu requires llama-server on PATH" >&2
      exit 69
    fi
    exec llama-server \
      --model "${model_path}" \
      --alias "${model_alias}" \
      --host "${host}" \
      --port "${port}" \
      --ctx-size "${n_ctx}" \
      --threads "${n_threads}"
    ;;
  *)
    echo "unknown local runtime profile: ${profile}" >&2
    exit 64
    ;;
esac
