#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

image_ref="${1:?image reference is required}"
launch_mode="${HEARTWOOD_TERRA_JUPYTER_MODE:-entrypoint}"
container_name="${2:-heartwood-terra-jupyter-smoke-${launch_mode}}"
docker_platform="${HEARTWOOD_TERRA_DOCKER_PLATFORM:-linux/amd64}"
host_port="${HEARTWOOD_TERRA_HOST_PORT:-8000}"
google_project="${HEARTWOOD_TERRA_GOOGLE_PROJECT:-heartwood-ci}"
cluster_name="${HEARTWOOD_TERRA_CLUSTER_NAME:-terra-smoke}"
root_url="${HEARTWOOD_TERRA_ROOT_URL:-http://127.0.0.1:${host_port}/}"
gateway_port="${HEARTWOOD_TERRA_GATEWAY_PORT:-8767}"
project_root="${HEARTWOOD_TERRA_PROJECT_ROOT:-/home/jupyter/terra-smoke-project}"

if [ "${launch_mode}" = "leonardo" ]; then
  notebook_path="/notebooks/${google_project}/${cluster_name}/"
else
  notebook_path="/notebooks/"
fi
notebook_url="${HEARTWOOD_TERRA_NOTEBOOK_URL:-http://127.0.0.1:${host_port}${notebook_path}}"
heartwood_url="${notebook_url}proxy/${gateway_port}/"

cleanup() {
  docker rm --force "${container_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

dump_logs() {
  docker logs "${container_name}" >&2 || true
  docker exec "${container_name}" sh -c 'test -f "${HOME}/jupyter.log" && tail -200 "${HOME}/jupyter.log"' >&2 || true
  docker exec "${container_name}" sh -c 'test -f /tmp/heartwood-web.log && tail -200 /tmp/heartwood-web.log' >&2 || true
}

cleanup
if [ "${launch_mode}" = "leonardo" ]; then
  docker run \
    --detach \
    --name "${container_name}" \
    --platform "${docker_platform}" \
    --env "GOOGLE_PROJECT=${google_project}" \
    --env "CLUSTER_NAME=${cluster_name}" \
    --publish "127.0.0.1:${host_port}:8000" \
    --entrypoint /etc/jupyter/scripts/run-jupyter.sh \
    "${image_ref}" \
    /home/jupyter >/dev/null
else
  docker run \
    --detach \
    --name "${container_name}" \
    --platform "${docker_platform}" \
    --publish "127.0.0.1:${host_port}:8000" \
    "${image_ref}" >/dev/null
fi

for _ in $(seq 1 60); do
  if curl --fail --silent "${notebook_url}" >/dev/null; then
    break
  fi
  sleep 1
done

if ! curl --fail --silent --show-error "${notebook_url}" >/dev/null; then
  dump_logs
  exit 1
fi
root_status="$(curl --silent --output /dev/null --write-out '%{http_code}' "${root_url}")"
if [ "${root_status}" != "404" ]; then
  echo "expected ${root_url} to return Jupyter-style 404, got ${root_status}" >&2
  dump_logs
  exit 1
fi

docker exec "${container_name}" mkdir -p "${project_root}"
docker exec --detach --workdir "${project_root}" "${container_name}" \
  sh -c "exec heartwood --interface web --host 0.0.0.0 --port ${gateway_port} > /tmp/heartwood-web.log 2>&1"

for _ in $(seq 1 60); do
  if curl --fail --silent "${heartwood_url}" >/dev/null; then
    break
  fi
  sleep 1
done

heartwood_html="$(curl --fail --silent --show-error "${heartwood_url}")"
if ! grep -q '<div id="root"></div>' <<<"${heartwood_html}"; then
  echo "Heartwood web UI did not load through ${heartwood_url}" >&2
  dump_logs
  exit 1
fi

readiness="$(curl --fail --silent --show-error "${heartwood_url}project/readiness")"
HEARTWOOD_EXPECTED_PROJECT="${project_root}" python3 -c '
import json
import os
import sys

payload = json.load(sys.stdin)
expected = os.environ["HEARTWOOD_EXPECTED_PROJECT"]
observed = payload.get("project_root")
if observed != expected:
    raise SystemExit(
        f"Heartwood proxy resolved {observed!r}, expected {expected!r}"
    )
' <<<"${readiness}"

echo "Terra Jupyter and Heartwood proxy smoke (${launch_mode}): ok"
