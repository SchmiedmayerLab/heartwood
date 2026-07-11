#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
workspace="${HEARTWOOD_WORKSPACE:-/tmp/heartwood-capable/sessions}"
state_root="$(dirname "${workspace}")"
session_id="${HEARTWOOD_SESSION_ID:-session-capable-model}"
settings="${HEARTWOOD_MODEL_SETTINGS:-${state_root}/models.json}"
action_settings="${HEARTWOOD_ACTION_SETTINGS:-${state_root}/actions.json}"
transcript="${HEARTWOOD_TRANSCRIPT:-${state_root}/transcript.txt}"
runtime_log="${HEARTWOOD_RUNTIME_LOG:-${state_root}/llama-server.log}"
command_timeout="${HEARTWOOD_COMMAND_TIMEOUT:-600}"
expected_content="heartwood-capable-model-ok"
proof_path="${state_root}/workspaces/${session_id}/model-proof.txt"
events_path="${workspace}/${session_id}/events.jsonl"

export HEARTWOOD_AGENT_BACKEND="openhands-sdk"
export HEARTWOOD_WORKSPACE="${workspace}"
export HEARTWOOD_SESSION_ID="${session_id}"
export HEARTWOOD_MODEL_SETTINGS="${settings}"
export HEARTWOOD_ACTION_SETTINGS="${action_settings}"
export HEARTWOOD_LOCAL_RUNTIME_PROFILE="llama-cpp-cpu"
export HEARTWOOD_LOCAL_MODEL_PATH="${model_path}"
export HEARTWOOD_LOCAL_MODEL_ALIAS="heartwood-local-runtime"
export HEARTWOOD_LOCAL_MODEL_CONTEXT="${HEARTWOOD_LOCAL_MODEL_CONTEXT:-8192}"
export HEARTWOOD_LOCAL_MODEL_THREADS="${HEARTWOOD_LOCAL_MODEL_THREADS:-8}"
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export OPENHANDS_SUPPRESS_BANNER="1"

rm -rf "${workspace}" "${state_root}/openhands" "${state_root}/workspaces"
rm -f "${settings}" "${action_settings}" "${transcript}" "${runtime_log}"
mkdir -p "${state_root}"

bash images/generic/scripts/start_local_runtime.sh >"${runtime_log}" 2>&1 &
runtime_pid="$!"

cleanup() {
  kill "${runtime_pid}" >/dev/null 2>&1 || true
  wait "${runtime_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

python - <<'PY'
import socket
import time

deadline = time.time() + 180
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.5):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.5)
raise SystemExit("mounted capable-model runtime did not become ready")
PY

run_heartwood() {
  timeout "${command_timeout}" heartwood "$@"
}

run_heartwood --workspace "${workspace}" models add capable-local \
  --model openai/heartwood-local-runtime \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" models validate capable-local | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" actions set auto-approve-low-risk | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" --session-id "${session_id}" chat \
  --prompt "Call the terminal tool exactly once. In that one call, execute this exact command: printf %s '${expected_content}' > model-proof.txt. Do not use echo, do not add a newline, and do not describe the command as text. Wait for the terminal result before calling any completion or finish action, and do not write the file again. After the command succeeds, report completion." \
  | tee -a "${transcript}"

for _ in 1 2 3 4; do
  pending_id="$(python - "${events_path}" <<'PY'
import json
import sys
from pathlib import Path

events_path = Path(sys.argv[1])
events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
resolved = {
    event["payload"].get("tool_call_id")
    for event in events
    if event["kind"] == "confirmation.resolved"
}
pending = [
    event["payload"]["request"]["tool_call_id"]
    for event in events
    if event["kind"] == "confirmation.requested"
    and event["payload"]["request"]["tool_call_id"] not in resolved
]
print(pending[-1] if pending else "")
PY
)"
  if [[ -z "${pending_id}" ]]; then
    break
  fi
  run_heartwood --workspace "${workspace}" --session-id "${session_id}" allow "${pending_id}" \
    | tee -a "${transcript}"
done

run_heartwood --workspace "${workspace}" --session-id "${session_id}" audit export \
  --output "${state_root}/audit-export.jsonl" | tee -a "${transcript}"

python - "${events_path}" "${proof_path}" "${expected_content}" <<'PY'
import json
import sys
from pathlib import Path

events_path = Path(sys.argv[1])
proof_path = Path(sys.argv[2])
expected_content = sys.argv[3]
events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
kinds = {event["kind"] for event in events}
required = {
    "model_call.decision.recorded",
    "tool.execution.recorded",
    "tool_call.proposed",
}
missing = required - kinds
if missing:
    raise SystemExit(f"capable-model session is missing events: {sorted(missing)}")
errors = [event["payload"].get("reason", "unknown") for event in events if event["kind"] == "error.recorded"]
if errors:
    raise SystemExit(f"capable-model session recorded errors: {errors}")
unresolved = {
    event["payload"]["request"]["tool_call_id"]
    for event in events
    if event["kind"] == "confirmation.requested"
} - {
    event["payload"].get("tool_call_id")
    for event in events
    if event["kind"] == "confirmation.resolved"
}
if unresolved:
    raise SystemExit(f"capable-model session has unresolved actions: {sorted(unresolved)}")
terminal_executions = [
    event
    for event in events
    if event["kind"] == "tool.execution.recorded"
    and event["payload"].get("tool_name") == "terminal"
]
if len(terminal_executions) != 1 or terminal_executions[0]["payload"].get("exit_code") != 0:
    raise SystemExit("capable-model session must have exactly one successful terminal execution")
if not proof_path.is_file():
    raise SystemExit(f"capable model did not create {proof_path}")
if proof_path.read_text(encoding="utf-8") != expected_content:
    raise SystemExit("capable model created unexpected proof content")
decisions = [
    event["payload"]["decision"]["decision"]
    for event in events
    if event["kind"] == "model_call.decision.recorded"
]
if not decisions or set(decisions) != {"allow"}:
    raise SystemExit(f"capable-model route was not consistently allowed: {decisions}")
confirmation_modes = {
    event["payload"]["model_profile"]["action_confirmation_mode"]
    for event in events
    if event["kind"] == "model_call.decision.recorded"
}
if confirmation_modes != {"confirm-risky"}:
    raise SystemExit(f"unexpected action confirmation modes: {sorted(confirmation_modes)}")
print("Mounted capable-model OpenHands end-to-end test: ok")
PY
