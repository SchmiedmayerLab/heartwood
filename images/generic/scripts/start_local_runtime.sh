#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

profile="${HEARTWOOD_LOCAL_RUNTIME_PROFILE:-llama-cpp-cpu}"
socket_path="${HEARTWOOD_LOCAL_RUNTIME_SOCKET:?HEARTWOOD_LOCAL_RUNTIME_SOCKET is required}"
heartwood_python="${HEARTWOOD_PYTHON:-python}"
request_log="${HEARTWOOD_MODEL_REQUEST_LOG:-/tmp/heartwood-local-model-requests.jsonl}"
model_path="${HEARTWOOD_LOCAL_MODEL_PATH:-}"
model_alias="${HEARTWOOD_MANAGED_MODEL_ALIAS:-heartwood-managed-runtime}"
default_threads="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
n_ctx="${HEARTWOOD_LOCAL_MODEL_CONTEXT:-32768}"
n_threads="${HEARTWOOD_LOCAL_MODEL_THREADS:-${default_threads}}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

"${heartwood_python}" - <<'PYTHON'
import os
from heartwood.gateway._managed_inference import runtime_socket_path
from heartwood.persistence import require_private_socket_parent
path = runtime_socket_path(os.environ)
require_private_socket_parent(path)
if path.exists() or path.is_symlink():
    raise SystemExit("runtime socket path already exists")
PYTHON
umask 077

case "${profile}" in
  stub-unix)
    exec "${heartwood_python}" "${script_dir}/local_model_stub.py" \
      --socket "${socket_path}" \
      --request-log "${request_log}"
    ;;
  llama-cpp-cpu)
    if [[ -z "${model_path}" || ! -f "${model_path}" ]]; then
      echo "llama-cpp-cpu requires HEARTWOOD_LOCAL_MODEL_PATH to reference a mounted or downloaded GGUF file" >&2
      exit 66
    fi
    if ! command -v llama-server >/dev/null 2>&1; then
      echo "managed runtime profile llama-cpp-cpu requires llama-server on PATH" >&2
      exit 69
    fi
    exec llama-server \
      --model "${model_path}" \
      --jinja \
      --alias "${model_alias}" \
      --host "${socket_path}" \
      --no-slots \
      --ctx-size "${n_ctx}" \
      --threads "${n_threads}"
    ;;
  *)
    echo "unknown managed runtime profile: ${profile}" >&2
    exit 64
    ;;
esac
