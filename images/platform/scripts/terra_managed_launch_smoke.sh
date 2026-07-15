#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

project_root="${HEARTWOOD_TERRA_PROJECT_ROOT:-${PWD}}"
port="${HEARTWOOD_TERRA_GATEWAY_PORT:-8767}"
startup_timeout="${HEARTWOOD_TERRA_STARTUP_TIMEOUT:-180}"
log_file="$(mktemp "${TMPDIR:-/tmp}/heartwood-terra-managed-launch.XXXXXX.log")"
readiness_file="$(mktemp "${TMPDIR:-/tmp}/heartwood-terra-managed-readiness.XXXXXX.json")"
launch_pid=""

cleanup() {
  status="$?"
  if [ -n "${launch_pid}" ]; then
    kill "${launch_pid}" >/dev/null 2>&1 || true
    wait "${launch_pid}" >/dev/null 2>&1 || true
  fi
  if [ "${status}" -ne 0 ]; then
    printf 'Managed Terra launch log:\n' >&2
    tail -n 200 "${log_file}" >&2 || true
  fi
  rm -f "${log_file}" "${readiness_file}"
  return "${status}"
}
trap cleanup EXIT

cd "${project_root}"
heartwood launch --web --host 127.0.0.1 --port "${port}" \
  --startup-timeout "${startup_timeout}" >"${log_file}" 2>&1 &
launch_pid="$!"

ready=""
for _ in $(seq 1 "${startup_timeout}"); do
  if ! kill -0 "${launch_pid}" >/dev/null 2>&1; then
    break
  fi
  if curl --fail --silent --show-error \
    "http://127.0.0.1:${port}/project/readiness" >"${readiness_file}"; then
    if HEARTWOOD_EXPECTED_PROJECT="${project_root}" python3 - "${readiness_file}" <<'PY'
import json
import os
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = Path(os.environ["HEARTWOOD_EXPECTED_PROJECT"]).resolve()
observed = Path(str(payload.get("project_root"))).resolve()
if observed != expected:
    raise SystemExit(f"managed launch resolved {observed}, expected {expected}")
if payload.get("platform_id") != "terra":
    raise SystemExit(f"managed launch selected {payload.get('platform_id')!r}, expected 'terra'")
if payload.get("state") != "ready":
    raise SystemExit(f"managed launch readiness is {payload.get('state')!r}, expected 'ready'")
PY
    then
      ready="yes"
      break
    fi
  fi
  sleep 1
done

if [ "${ready}" != "yes" ]; then
  echo "Heartwood managed Terra launch did not become ready." >&2
  exit 1
fi

grep --fixed-strings '[6/6] Open the web interface on 127.0.0.1:' "${log_file}" >/dev/null
echo "Terra managed local-model launch smoke: ok"
