#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

profile="${HEARTWOOD_LOCAL_RUNTIME_PROFILE:-stub-loopback}"
host="${HEARTWOOD_LOCAL_RUNTIME_HOST:-127.0.0.1}"
port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
request_log="${HEARTWOOD_MODEL_REQUEST_LOG:-/tmp/heartwood-local-model-requests.jsonl}"
model_path="${HEARTWOOD_LOCAL_MODEL_PATH:-/opt/heartwood/local-runtime/models/model.gguf}"

if [[ "${host}" != "127.0.0.1" && "${host}" != "localhost" && "${host}" != "::1" ]]; then
  echo "local runtime must bind to loopback, got ${host}" >&2
  exit 64
fi

case "${profile}" in
  stub-loopback)
    exec python images/generic/scripts/local_model_stub.py \
      --host "${host}" \
      --port "${port}" \
      --request-log "${request_log}"
    ;;
  llama-cpp-cpu)
    if [[ ! -f "${model_path}" ]]; then
      echo "local runtime profile llama-cpp-cpu requires HEARTWOOD_LOCAL_MODEL_PATH=${model_path}" >&2
      exit 66
    fi
    python - <<'PY'
import importlib.util
import sys

if importlib.util.find_spec("llama_cpp.server") is None:
    sys.stderr.write("local runtime profile llama-cpp-cpu requires llama-cpp-python server support\n")
    raise SystemExit(69)
PY
    exec python -m llama_cpp.server \
      --model "${model_path}" \
      --host "${host}" \
      --port "${port}"
    ;;
  *)
    echo "unknown local runtime profile: ${profile}" >&2
    exit 64
    ;;
esac
