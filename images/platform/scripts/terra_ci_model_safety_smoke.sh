#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

project_root="${HEARTWOOD_TERRA_PROJECT_ROOT:-${PWD}}"
artifact_path="${project_root}/.heartwood/models/llama-cpp-stories260k-ci/tinyllamas/stories260K.gguf"
readiness_file="$(mktemp "${TMPDIR:-/tmp}/heartwood-terra-model-safety.XXXXXX.json")"

cleanup() {
  status="$?"
  rm -f "${readiness_file}"
  return "${status}"
}
trap cleanup EXIT

cd "${project_root}"
test -f "${artifact_path}"
heartwood doctor --json >"${readiness_file}"

HEARTWOOD_EXPECTED_PROJECT="${project_root}" python3 - "${readiness_file}" <<'PY'
import json
import os
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = Path(os.environ["HEARTWOOD_EXPECTED_PROJECT"]).resolve()
observed = Path(str(payload.get("project_root"))).resolve()
if observed != expected:
    raise SystemExit(f"model safety check resolved {observed}, expected {expected}")
if payload.get("platform_id") != "terra":
    raise SystemExit(
        f"model safety check selected {payload.get('platform_id')!r}, expected 'terra'"
    )
if payload.get("state") != "setup-required":
    raise SystemExit(
        f"model safety readiness is {payload.get('state')!r}, expected 'setup-required'; "
        "the CI-only model must not become an agent profile"
    )
checks = {item["check_id"]: item for item in payload.get("checks", [])}
if checks.get("terra-project-storage", {}).get("status") != "pass":
    raise SystemExit("model safety check did not confirm Terra persistent project storage")
if checks.get("terra-gpu", {}).get("status") != "pass":
    raise SystemExit("model safety check did not confirm the portable Terra runtime")
if checks.get("model", {}).get("summary") != "No active model selected":
    raise SystemExit("the CI-only model was incorrectly promoted to an active agent profile")
if checks.get("model-source", {}).get("summary") != "No model connection selected":
    raise SystemExit("the CI-only model incorrectly configured an agent model connection")
PY

echo "Terra CI-only model safety smoke: ok"
