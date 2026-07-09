#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

host="${HEARTWOOD_AGENT_SERVER_HOST:-127.0.0.1}"
port="${HEARTWOOD_AGENT_SERVER_PORT:-8766}"
workspace_root="${HEARTWOOD_AGENT_SERVER_WORKSPACE:-/tmp/heartwood-openhands}"
session_api_key="${HEARTWOOD_AGENT_SERVER_API_KEY:-}"

if [[ "${host}" != "127.0.0.1" && "${host}" != "localhost" && "${host}" != "::1" ]]; then
  echo "agent-server must bind to loopback, got ${host}" >&2
  exit 64
fi

if [[ -z "${session_api_key}" ]]; then
  session_api_key="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

python - <<'PY'
import shutil
import sys

if shutil.which("agent-server") is None:
    sys.stderr.write("OpenHands agent-server command is not installed\n")
    raise SystemExit(69)
PY

mkdir -p "${workspace_root}/conversations" \
  "${workspace_root}/project" \
  "${workspace_root}/bash-events"

export OH_CONVERSATIONS_PATH="${OH_CONVERSATIONS_PATH:-${workspace_root}/conversations}"
export OH_WORKSPACE_PATH="${OH_WORKSPACE_PATH:-${workspace_root}/project}"
export OH_BASH_EVENTS_DIR="${OH_BASH_EVENTS_DIR:-${workspace_root}/bash-events}"
export OH_ENABLE_VSCODE="${OH_ENABLE_VSCODE:-false}"
export OH_ENABLE_VNC="${OH_ENABLE_VNC:-false}"
export OH_PRELOAD_TOOLS="${OH_PRELOAD_TOOLS:-false}"
export OH_SESSION_API_KEYS_0="${OH_SESSION_API_KEYS_0:-${session_api_key}}"
export OH_SECRET_KEY="${OH_SECRET_KEY:-${session_api_key}}"
export OPENHANDS_SUPPRESS_BANNER="${OPENHANDS_SUPPRESS_BANNER:-1}"

exec agent-server --host "${host}" --port "${port}"
