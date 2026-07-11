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
runtime_root="${HEARTWOOD_RUNTIME_ROOT:-/opt/heartwood}"
runtime_port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
cohort_path="${state_root}/workspaces/${session_id}/cohort-summary.json"
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
export HEARTWOOD_LOCAL_RUNTIME_PORT="${runtime_port}"
export HEARTWOOD_RUNTIME_ROOT="${runtime_root}"
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export OPENHANDS_SUPPRESS_BANNER="1"

rm -rf "${workspace}" "${state_root}/openhands" "${state_root}/workspaces"
rm -f "${settings}" "${action_settings}" "${transcript}" "${runtime_log}"
mkdir -p "${state_root}/workspaces/${session_id}/input"
cp "${runtime_root}/fixtures/synthetic/omop-like/"*.csv \
  "${state_root}/workspaces/${session_id}/input/"

bash images/generic/scripts/start_local_runtime.sh >"${runtime_log}" 2>&1 &
runtime_pid="$!"

cleanup() {
  kill "${runtime_pid}" >/dev/null 2>&1 || true
  wait "${runtime_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

python - <<'PY'
import os
import time
import urllib.error
import urllib.request

port = int(os.environ["HEARTWOOD_LOCAL_RUNTIME_PORT"])
deadline = time.time() + 180
last_error = None
while time.time() < deadline:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as response:
            if response.status == 200:
                break
    except (OSError, urllib.error.URLError) as error:
        last_error = error
        time.sleep(0.2)
else:
    raise SystemExit(f"mounted capable-model runtime did not become ready: {last_error}")
PY

run_heartwood() {
  timeout "${command_timeout}" heartwood "$@"
}

run_heartwood --workspace "${workspace}" models refresh local | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" models connect local heartwood-local-runtime \
  | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" models validate local | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" actions set auto-approve-low-risk | tee -a "${transcript}"
run_heartwood --workspace "${workspace}" --session-id "${session_id}" chat \
  --prompt "Call the terminal tool exactly once. In that one call, execute this exact command: python ${runtime_root}/skills/verified/omop-cohort-summary/scripts/run.py --data-root input --target-condition-concept-id 201826 --minimum-age 18 --aggregate-count-floor 20 --output cohort-summary.json. Do not describe the command as text. Wait for the terminal result before calling any completion or finish action, and do not write the file again. After the command succeeds, report the aggregate cohort result." \
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

python - "${events_path}" "${cohort_path}" <<'PY'
import json
import sys
from pathlib import Path

events_path = Path(sys.argv[1])
cohort_path = Path(sys.argv[2])
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
if not cohort_path.is_file():
    raise SystemExit(f"capable model did not create {cohort_path}")
cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
summary = cohort["summary"]
if summary["source_participant_count"] != 24 or summary["participant_count"] != 20:
    raise SystemExit(f"capable model produced an unexpected reference cohort: {summary}")
if summary["source_condition_occurrence_count"] != 39:
    raise SystemExit(f"capable model produced an unexpected source condition count: {summary}")
if summary["condition_occurrence_count"] != 35:
    raise SystemExit(f"capable model produced an unexpected condition count: {summary}")
if not cohort["export_guard"]["exportable"]:
    raise SystemExit("capable model reference cohort unexpectedly failed the count floor")
checks = cohort["quality_checks"]
if checks["row_values_exported"] is not False:
    raise SystemExit("capable model reference artifact contains row-level output")
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
