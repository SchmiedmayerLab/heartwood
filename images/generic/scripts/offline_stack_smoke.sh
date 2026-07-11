#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

workspace="${HEARTWOOD_WORKSPACE:-/tmp/heartwood-sessions}"
state_root="$(dirname "${workspace}")"
session_id="${HEARTWOOD_SESSION_ID:-session-offline-stack}"
rejected_session_id="${session_id}-rejected"
automatic_session_id="${session_id}-automatic"
risky_session_id="${session_id}-risky"
settings="${HEARTWOOD_MODEL_SETTINGS:-/tmp/heartwood-models.json}"
action_settings="${HEARTWOOD_ACTION_SETTINGS:-/tmp/heartwood-actions.json}"
request_log="${HEARTWOOD_MODEL_REQUEST_LOG:-/tmp/heartwood-local-model-requests.jsonl}"
audit_copy="${HEARTWOOD_AUDIT_EXPORT:-/tmp/heartwood-audit-export.jsonl}"
transcript="${HEARTWOOD_TRANSCRIPT:-/tmp/heartwood-offline-transcript.txt}"
automatic_transcript="${HEARTWOOD_AUTOMATIC_TRANSCRIPT:-/tmp/heartwood-automatic-transcript.txt}"
risky_transcript="${HEARTWOOD_RISKY_TRANSCRIPT:-/tmp/heartwood-risky-transcript.txt}"
heartwood_python="${HEARTWOOD_PYTHON:-python}"

export HEARTWOOD_AGENT_BACKEND="openhands-sdk"
export HEARTWOOD_WORKSPACE="${workspace}"
export HEARTWOOD_SESSION_ID="${session_id}"
export HEARTWOOD_MODEL_SETTINGS="${settings}"
export HEARTWOOD_ACTION_SETTINGS="${action_settings}"
export HEARTWOOD_MODEL_REQUEST_LOG="${request_log}"
export HEARTWOOD_UNUSED_MODEL_API_KEY="synthetic-unused-model-key"
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export OPENHANDS_SUPPRESS_BANNER="1"

rm -rf "${workspace}" "${state_root}/openhands" "${state_root}/workspaces"
rm -f "${settings}" "${action_settings}" "${request_log}" "${audit_copy}" "${transcript}" \
  "${automatic_transcript}" "${risky_transcript}"

HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback \
  bash images/generic/scripts/start_local_runtime.sh &
model_pid="$!"

cleanup() {
  kill "${model_pid}" >/dev/null 2>&1 || true
  wait "${model_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${heartwood_python}" - <<'PY'
import socket
import time

deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.2):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.1)
raise SystemExit("loopback model fixture did not become ready")
PY

heartwood --workspace "${workspace}" models add local-smoke \
  --model openai/heartwood-local-runtime \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select | tee -a "${transcript}"
heartwood --workspace "${workspace}" models add inactive-smoke \
  --model openai/heartwood-inactive-runtime \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind environment \
  --api-key-env HEARTWOOD_UNUSED_MODEL_API_KEY | tee -a "${transcript}"
heartwood --workspace "${workspace}" models validate local-smoke | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${session_id}" detect | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${session_id}" chat \
  --prompt "Respond to this synthetic offline integration check." | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${session_id}" allow \
  call-heartwood-offline-smoke | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${rejected_session_id}" chat \
  --prompt "Propose the bounded synthetic action for rejection." | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${rejected_session_id}" reject \
  call-heartwood-offline-smoke | tee -a "${transcript}"
heartwood --workspace "${workspace}" actions set auto-approve-low-risk | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${automatic_session_id}" chat \
  --prompt "Run the bounded low-risk automatic integration check." \
  | tee "${automatic_transcript}" | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${risky_session_id}" chat \
  --prompt "Propose the medium-risk network check for review." \
  | tee "${risky_transcript}" | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${risky_session_id}" reject \
  call-heartwood-offline-smoke | tee -a "${risky_transcript}" | tee -a "${transcript}"
heartwood --workspace "${workspace}" --session-id "${session_id}" audit export \
  --output "${audit_copy}" | tee -a "${transcript}"

"${heartwood_python}" - <<'PY'
from pathlib import Path

from openhands.sdk.skills import load_skills_from_dir

repository, knowledge, agent = load_skills_from_dir(Path("skills/verified"))
names = set(repository) | set(knowledge) | set(agent)
expected = {"aggregate-export", "baseline-model", "omop-cohort-summary"}
if not expected.issubset(names):
    raise SystemExit(f"verified skills were not loaded by OpenHands: {sorted(names)}")
PY

test -s "${request_log}"
grep -q '"path": "/v1/chat/completions"' "${request_log}"
grep -q "Policy: allow" "${transcript}"
grep -q "Action: run a bounded offline smoke command" "${transcript}"
grep -q "Allow once or reject" "${transcript}"
grep -q "Action approved" "${transcript}"
grep -q "Tool terminal exit=0" "${transcript}"
grep -q "Action denied" "${transcript}"
grep -q "Agent: Synthetic local model response." "${transcript}"
grep -q "Tool terminal exit=0" "${automatic_transcript}"
if grep -q "Allow once or reject" "${automatic_transcript}"; then
  echo "low-risk action unexpectedly required confirmation" >&2
  exit 1
fi
grep -q "Action: run a medium-risk network command (risk=medium)" "${risky_transcript}"
grep -q "Allow once or reject" "${risky_transcript}"
grep -q "Action denied" "${risky_transcript}"
"${heartwood_python}" - <<'PY'
import json
import os
from pathlib import Path

workspace = Path(os.environ.get("HEARTWOOD_WORKSPACE", "/tmp/heartwood-sessions"))
session_id = os.environ.get("HEARTWOOD_SESSION_ID", "session-offline-stack") + "-automatic"
events = [
    json.loads(line)
    for line in (workspace / session_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
]
kinds = {event["kind"] for event in events}
if "confirmation.requested" in kinds:
    raise SystemExit("low-risk automatic session contains a confirmation request")
if not {"tool_call.proposed", "tool.execution.recorded"}.issubset(kinds):
    raise SystemExit(f"low-risk automatic session is incomplete: {sorted(kinds)}")
decision = next(event for event in events if event["kind"] == "model_call.decision.recorded")
if decision["payload"]["model_profile"]["action_confirmation_mode"] != "confirm-risky":
    raise SystemExit("action confirmation mode was not recorded")
PY
test -s "${audit_copy}"
heartwood --workspace "${workspace}" actions set ask-every-time | tee -a "${transcript}"
"${heartwood_python}" images/generic/scripts/terra_jupyter_demo_smoke.py | tee -a "${transcript}"
grep -q "Terra-style Jupyter demo smoke: ok" "${transcript}"
