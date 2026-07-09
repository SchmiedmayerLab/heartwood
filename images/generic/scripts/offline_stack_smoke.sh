#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

workspace="${HEARTWOOD_WORKSPACE:-/tmp/heartwood-sessions}"
session_id="${HEARTWOOD_SESSION_ID:-session-offline-stack}"
runtime_profile="${HEARTWOOD_LOCAL_RUNTIME_PROFILE:-stub-loopback}"
model_endpoint="${HEARTWOOD_LOCAL_MODEL_ENDPOINT:-http://127.0.0.1:8765/v1/chat}"
request_log="${HEARTWOOD_MODEL_REQUEST_LOG:-/tmp/heartwood-local-model-requests.jsonl}"
audit_copy="${HEARTWOOD_AUDIT_EXPORT:-/tmp/heartwood-audit-export.jsonl}"
reviewer_output="${HEARTWOOD_REVIEWER_PACKET:-/tmp/heartwood-reviewer-packet}"
transcript="${HEARTWOOD_TRANSCRIPT:-/tmp/heartwood-offline-transcript.txt}"

rm -rf "${workspace}" "${reviewer_output}"
rm -f "${request_log}" "${audit_copy}" "${transcript}"

HEARTWOOD_LOCAL_RUNTIME_PROFILE="${runtime_profile}" \
HEARTWOOD_MODEL_REQUEST_LOG="${request_log}" \
  bash images/generic/scripts/start_local_runtime.sh &
model_pid="$!"

cleanup() {
  kill "${model_pid}" >/dev/null 2>&1 || true
  wait "${model_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

python - <<'PY'
import socket
import time

deadline = time.time() + 5
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.1)
raise SystemExit("local runtime did not become ready")
PY

heartwood --workspace "${workspace}" --session-id "${session_id}" detect | tee -a "${transcript}"
heartwood \
  --workspace "${workspace}" \
  --session-id "${session_id}" \
  approve \
  --target-type model-call \
  --target-id decision-synthetic-model-call | tee -a "${transcript}"
heartwood \
  --workspace "${workspace}" \
  --session-id "${session_id}" \
  run \
  --local-model \
  --endpoint "${model_endpoint}" \
  --prompt "run the synthetic offline stack" | tee -a "${transcript}"
heartwood \
  --workspace "${workspace}" \
  --session-id "${session_id}" \
  audit export \
  --output "${audit_copy}" | tee -a "${transcript}"
heartwood \
  --workspace "${workspace}" \
  --session-id "${session_id}" \
  reviewer packet \
  --output "${reviewer_output}" | tee -a "${transcript}"

if [[ "${runtime_profile}" == "stub-loopback" ]]; then
  test -s "${request_log}"
  grep -q '"path": "/v1/chat"' "${request_log}"
fi
test -s "${audit_copy}"
test -s "${reviewer_output}/reviewer-packet.md"
grep -q "model=heartwood-local-demo status=ok" "${transcript}"
grep -q "Tool execution: heartwood.synthetic.noop exit=0" "${transcript}"
