#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

workspace="${HEARTWOOD_WORKSPACE:-/tmp/heartwood-web-session}"
host="${HEARTWOOD_WEB_HOST:-127.0.0.1}"
port="${HEARTWOOD_WEB_PORT:-8767}"
web_root="${HEARTWOOD_WEB_ROOT:-/opt/heartwood/packages/webui/dist}"
base_path="${HEARTWOOD_WEB_BASE_PATH:-/}"
agent_backend="${HEARTWOOD_AGENT_BACKEND:-deterministic-local}"

if [[ "${agent_backend}" == "openhands-bash" || "${agent_backend}" == "openhands-agent-server" ]]; then
  export HEARTWOOD_AGENT_SERVER_ENABLED="${HEARTWOOD_AGENT_SERVER_ENABLED:-1}"
  export HEARTWOOD_AGENT_SERVER_COMMAND="${HEARTWOOD_AGENT_SERVER_COMMAND:-bash images/generic/scripts/start_agent_server.sh}"
  export HEARTWOOD_AGENT_SERVER_READY_TIMEOUT_SECONDS="${HEARTWOOD_AGENT_SERVER_READY_TIMEOUT_SECONDS:-180}"
  export HEARTWOOD_AGENT_SERVER_WORKSPACE="${HEARTWOOD_AGENT_SERVER_WORKSPACE:-/tmp/heartwood-openhands}"
fi

mkdir -p "${workspace}"

exec heartwood \
  --workspace "${workspace}" \
  serve \
  --host "${host}" \
  --port "${port}" \
  --web-root "${web_root}" \
  --base-path "${base_path}"
