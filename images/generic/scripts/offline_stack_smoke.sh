#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

if ! command -v jq >/dev/null; then
  echo "Heartwood runtime requires jq for agent JSON inspection" >&2
  exit 69
fi

project="${HEARTWOOD_SMOKE_PROJECT:-/tmp/heartwood-offline-project}"
state_root="${project}/.heartwood"
workspace="${state_root}/sessions"
session_id="${HEARTWOOD_SESSION_ID:-session-offline-stack}"
rejected_session_id="${session_id}-rejected"
automatic_session_id="${session_id}-automatic"
risky_session_id="${session_id}-risky"
request_log="${HEARTWOOD_MODEL_REQUEST_LOG:-/tmp/heartwood-local-model-requests.jsonl}"
audit_copy="${HEARTWOOD_AUDIT_EXPORT:-/tmp/heartwood-audit-export.jsonl}"
transcript="${HEARTWOOD_TRANSCRIPT:-/tmp/heartwood-offline-transcript.txt}"
automatic_transcript="${HEARTWOOD_AUTOMATIC_TRANSCRIPT:-/tmp/heartwood-automatic-transcript.txt}"
risky_transcript="${HEARTWOOD_RISKY_TRANSCRIPT:-/tmp/heartwood-risky-transcript.txt}"
heartwood_python="${HEARTWOOD_PYTHON:-python}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="$(cd "${script_dir}/../../.." && pwd)"

export HEARTWOOD_SESSION_ID="${session_id}"
export HEARTWOOD_MODEL_REQUEST_LOG="${request_log}"
export HEARTWOOD_RUNTIME_ROOT="${runtime_root}"
export HEARTWOOD_UNUSED_MODEL_API_KEY="synthetic-unused-model-key"
export LITELLM_LOCAL_MODEL_COST_MAP="True"
export OPENHANDS_SUPPRESS_BANNER="1"

rm -rf "${project}"
rm -f "${request_log}" "${audit_copy}" "${transcript}" \
  "${automatic_transcript}" "${risky_transcript}"
mkdir -p "${project}/input"
cp "${runtime_root}/fixtures/synthetic/omop-like/"*.csv \
  "${project}/input/"
cd "${project}"

HEARTWOOD_LOCAL_RUNTIME_PROFILE=stub-loopback \
  bash "${runtime_root}/images/generic/scripts/start_local_runtime.sh" &
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

heartwood models refresh heartwood | tee -a "${transcript}"
heartwood models connect heartwood heartwood-managed-runtime \
  | tee -a "${transcript}"
heartwood models add inactive-smoke \
  --model openai/heartwood-inactive-runtime \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind environment \
  --api-key-env HEARTWOOD_UNUSED_MODEL_API_KEY | tee -a "${transcript}"
heartwood models validate heartwood | tee -a "${transcript}"
heartwood --session-id "${session_id}" \
  --prompt "Build the synthetic target-condition cohort for concept 201826 with the repository-verified cohort Skill. Use the localized OMOP reference tables, minimum age 18, aggregate count floor 20, and write cohort-summary.json. Report the cohort definition and quality checks without row-level values." \
  | tee -a "${transcript}"
heartwood --session-id "${session_id}" allow | tee -a "${transcript}"
heartwood --session-id "${rejected_session_id}" \
  --prompt "Propose the bounded synthetic action for rejection." | tee -a "${transcript}"
heartwood --session-id "${rejected_session_id}" reject | tee -a "${transcript}"
heartwood actions set auto-approve-low-risk | tee -a "${transcript}"
heartwood --session-id "${automatic_session_id}" \
  --prompt "Run the bounded low-risk automatic integration check." \
  | tee "${automatic_transcript}" | tee -a "${transcript}"
heartwood --session-id "${risky_session_id}" \
  --prompt "Propose the medium-risk network check for review." \
  | tee "${risky_transcript}" | tee -a "${transcript}"
heartwood --session-id "${risky_session_id}" reject \
  | tee -a "${risky_transcript}" | tee -a "${transcript}"
heartwood --session-id "${session_id}" audit export \
  --output "${audit_copy}" | tee -a "${transcript}"

"${heartwood_python}" - <<'PY'
import os
from pathlib import Path

from openhands.sdk.skills import load_skills_from_dir

repository, knowledge, agent = load_skills_from_dir(
    Path(os.environ["HEARTWOOD_RUNTIME_ROOT"]) / "skills" / "verified"
)
names = set(repository) | set(knowledge) | set(agent)
expected = {"aggregate-export", "baseline-model", "omop-cohort-summary"}
if not expected.issubset(names):
    raise SystemExit(f"verified skills were not loaded by OpenHands: {sorted(names)}")
PY

test -s "${request_log}"
grep -q '"path": "/v1/chat/completions"' "${request_log}"
grep -q "Policy: allow" "${transcript}"
grep -q "build the aggregate synthetic target-condition cohort" "${transcript}"
grep -q "as one OpenHands action set" "${transcript}"
grep -q "Allow all once: /allow" "${transcript}"
grep -q "Action set approved" "${transcript}"
grep -q "Tool terminal exit=0" "${transcript}"
grep -q "Action set denied" "${transcript}"
grep -q "Agent: The synthetic target-condition cohort summary is ready for review." "${transcript}"
grep -q "Tool terminal exit=0" "${automatic_transcript}"
if grep -q "as one OpenHands action set" "${automatic_transcript}"; then
  echo "low-risk action unexpectedly required confirmation" >&2
  exit 1
fi
grep -q "run a medium-risk network command \[tool=terminal, risk=medium\]" "${risky_transcript}"
grep -q "as one OpenHands action set" "${risky_transcript}"
grep -q "Reject all: /reject" "${risky_transcript}"
grep -q "Action set denied" "${risky_transcript}"
"${heartwood_python}" - <<'PY'
import json
import os
from pathlib import Path

workspace = Path.cwd() / ".heartwood" / "sessions"
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
"${heartwood_python}" - <<'PY'
import json
import os
from pathlib import Path

project = Path.cwd()
workspace = project / ".heartwood" / "sessions"
session_id = os.environ.get("HEARTWOOD_SESSION_ID", "session-offline-stack")
artifact = project / "cohort-summary.json"
payload = json.loads(artifact.read_text(encoding="utf-8"))
summary = payload["summary"]
checks = payload["quality_checks"]
if summary["source_participant_count"] != 24 or summary["participant_count"] != 20:
    raise SystemExit(f"unexpected reference cohort summary: {summary}")
if summary["source_condition_occurrence_count"] != 39:
    raise SystemExit(f"unexpected source condition count: {summary}")
if summary["condition_occurrence_count"] != 35:
    raise SystemExit(f"unexpected reference condition count: {summary}")
if not all(value for key, value in checks.items() if key != "row_values_exported"):
    raise SystemExit(f"reference analysis failed quality checks: {checks}")
if checks["row_values_exported"] is not False or not payload["export_guard"]["exportable"]:
    raise SystemExit("reference analysis violated aggregate output expectations")
PY
test -s "${audit_copy}"
heartwood actions set ask-every-time | tee -a "${transcript}"
"${heartwood_python}" "${runtime_root}/images/generic/scripts/terra_jupyter_demo_smoke.py" \
  | tee -a "${transcript}"
grep -q "Terra-style Jupyter demo smoke: ok" "${transcript}"
