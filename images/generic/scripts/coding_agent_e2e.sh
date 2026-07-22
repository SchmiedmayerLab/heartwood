#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

model_path="${HEARTWOOD_LOCAL_MODEL_PATH:?HEARTWOOD_LOCAL_MODEL_PATH is required}"
project="${HEARTWOOD_CAPABLE_PROJECT:-/tmp/heartwood-capable-project}"
if [[ "${project}" == "/" ]]; then
  echo "refusing to use the filesystem root as the coding-agent test project" >&2
  exit 64
fi
if [[ ! -e "${model_path}" ]]; then
  echo "coding-agent model is unavailable: ${model_path}" >&2
  exit 66
fi
mkdir -p "${project}"
project="$(cd "${project}" && pwd -P)"
if [[ -d "${model_path}" ]]; then
  model_path="$(cd "${model_path}" && pwd -P)"
else
  model_path="$(cd "$(dirname "${model_path}")" && pwd -P)/$(basename "${model_path}")"
fi
case "${model_path}" in
  "${project}"/*)
    echo "coding-agent model must be outside the disposable test project" >&2
    exit 64
    ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime_root="${HEARTWOOD_RUNTIME_ROOT:-$(cd "${script_dir}/../../.." && pwd)}"
heartwood_python="${HEARTWOOD_PYTHON:-${runtime_root}/.venv/bin/python}"
heartwood_cli="${HEARTWOOD_CLI:-${runtime_root}/.venv/bin/heartwood}"
state_root="${project}/.heartwood"
workspace="${state_root}/sessions"
session_id="${HEARTWOOD_SESSION_ID:-session-capable-model}"
transcript="${HEARTWOOD_TRANSCRIPT:-${project}/heartwood-transcript.txt}"
replay="${HEARTWOOD_REPLAY_TRANSCRIPT:-${project}/heartwood-replay.txt}"
report="${HEARTWOOD_QUALIFICATION_REPORT:-${project}/heartwood-qualification.json}"
inference="${project}/qualification-inference.json"
command_timeout="${HEARTWOOD_COMMAND_TIMEOUT:-900}"
runtime_port="${HEARTWOOD_LOCAL_RUNTIME_PORT:-8765}"
cohort_path="${project}/cohort-summary.json"
events_path="${workspace}/${session_id}/events.jsonl"
audit_path="${state_root}/audit-export.jsonl"

if [[ ! -x "${heartwood_python}" ]]; then
  echo "Heartwood Python is unavailable: ${heartwood_python}" >&2
  exit 69
fi
if [[ ! -x "${heartwood_cli}" ]]; then
  echo "Heartwood CLI is unavailable: ${heartwood_cli}" >&2
  exit 69
fi

export HEARTWOOD_SESSION_ID="${session_id}"
export HEARTWOOD_LOCAL_MODEL_PATH="${model_path}"
export HEARTWOOD_MANAGED_MODEL_ALIAS="${HEARTWOOD_MANAGED_MODEL_ALIAS:-heartwood-managed-runtime}"
export HEARTWOOD_LOCAL_RUNTIME_PORT="${runtime_port}"
export HEARTWOOD_RUNTIME_ROOT="${runtime_root}"
export LITELLM_LOCAL_MODEL_COST_MAP=True
export OPENHANDS_SUPPRESS_BANNER=1

rm -rf "${project}/input" "${state_root}"
mkdir -p "${project}/input"
rm -f "${cohort_path}" "${transcript}" "${replay}" "${report}"
cp "${runtime_root}/fixtures/synthetic/omop-like/"*.csv "${project}/input/"
cd "${project}"

echo "Checking direct model inference..."
"${heartwood_python}" - "${inference}" <<'PY'
import json
import os
import sys
import urllib.request

payload = json.dumps(
    {
        "model": os.environ["HEARTWOOD_MANAGED_MODEL_ALIAS"],
        "messages": [{"role": "user", "content": "Reply briefly that inference is ready."}],
        "max_tokens": 32,
        "temperature": 0,
    }
).encode()
request = urllib.request.Request(
    f"http://127.0.0.1:{os.environ['HEARTWOOD_LOCAL_RUNTIME_PORT']}/v1/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=300) as response:
    result = json.load(response)
message = result["choices"][0]["message"]
content = message.get("content") or message.get("reasoning_content")
if not isinstance(content, str) or not content.strip():
    raise SystemExit("direct model inference returned no content")
with open(sys.argv[1], "w", encoding="utf-8") as file:
    json.dump({"content_nonempty": True, "model": result.get("model")}, file)
    file.write("\n")
PY

run_heartwood() {
  timeout "${command_timeout}" "${heartwood_cli}" "$@"
}

run_heartwood models refresh heartwood | tee -a "${transcript}"
run_heartwood models connect heartwood heartwood-managed-runtime | tee -a "${transcript}"
run_heartwood models validate heartwood | tee -a "${transcript}"
run_heartwood actions set ask-every-time | tee -a "${transcript}"
run_heartwood --session-id "${session_id}" \
  --prompt "Call the terminal tool to execute this exact command: ${heartwood_python} ${runtime_root}/skills/verified/omop-cohort-summary/scripts/run.py --data-root input --target-condition-concept-id 201826 --minimum-age 18 --aggregate-count-floor 20 --output cohort-summary.json && cat cohort-summary.json. Do not describe the command as text and do not call another tool after it completes. Wait for the terminal result, then report the aggregate cohort result." \
  | tee -a "${transcript}"

for _ in 1 2 3 4; do
  pending_id="$("${heartwood_python}" - "${events_path}" <<'PY'
import json
import sys
from pathlib import Path

events = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()]
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
  run_heartwood --session-id "${session_id}" allow | tee -a "${transcript}"
done

run_heartwood --session-id "${session_id}" replay | tee "${replay}"
run_heartwood --session-id "${session_id}" audit export \
  --output "${audit_path}" | tee -a "${transcript}"

"${heartwood_python}" "${script_dir}/verify_coding_agent_e2e.py" \
  --events "${events_path}" \
  --audit "${audit_path}" \
  --artifact "${cohort_path}" \
  --replay "${replay}" \
  --inference "${inference}" \
  --report "${report}" \
  --root "${runtime_root}"
