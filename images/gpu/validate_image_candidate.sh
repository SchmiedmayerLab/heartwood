#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 IMAGE_REFERENCE TARGET EXPECTED_REVISION" >&2
  exit 64
fi

candidate="$1"
target="$2"
expected_revision="$3"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${target}" in
  runtime-gpu-nvidia | terra-runtime-gpu-nvidia) ;;
  *)
    echo "unsupported GPU image target: ${target}" >&2
    exit 64
    ;;
esac

docker pull --platform linux/amd64 "${candidate}"
"${script_dir}/../scripts/verify_image_revision.sh" \
  "${candidate}" linux/amd64 "${expected_revision}"

docker run --rm --platform linux/amd64 \
  --entrypoint /opt/heartwood/images/gpu/verify_runtime.sh "${candidate}"

profiles="$(
  docker run --rm --platform linux/amd64 \
    --entrypoint heartwood-python "${candidate}" \
    /opt/heartwood/images/gpu/qualification_config.py --list
)"
jq --exit-status '
  length > 0
  and all(.[].configuration_id; type == "string" and length > 0)
' <<<"${profiles}" >/dev/null

if [ "${target}" = "terra-runtime-gpu-nvidia" ]; then
  docker run --rm --platform linux/amd64 --network none \
    --entrypoint bash \
    "${candidate}" \
    /opt/heartwood/images/platform/scripts/terra_jupyter_contract_smoke.sh
  docker run --rm --platform linux/amd64 --network none \
    --entrypoint bash \
    "${candidate}" \
    -c 'mkdir -p /home/jupyter/synthetic-analysis &&
    cd /home/jupyter/synthetic-analysis &&
    exec /opt/heartwood/images/platform/scripts/terra_image_smoke.sh'
  docker run --rm --platform linux/amd64 --network none \
    --entrypoint bash \
    --env HEARTWOOD_SMOKE_PROJECT=/home/jupyter/synthetic-agent-analysis \
    --env HEARTWOOD_TERRA_DEMO_PROJECT_ROOT=/home/jupyter/synthetic-notebook-analysis \
    "${candidate}" \
    /opt/heartwood/images/generic/scripts/offline_stack_smoke.sh
else
  docker run --rm --init --platform linux/amd64 \
    --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges=true --pids-limit 256 \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g \
    --tmpfs /home/heartwood/.cache:rw,nosuid,nodev,size=512m,uid=10001,gid=10001,mode=0700 \
    --tmpfs /home/heartwood/.openhands:rw,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
    "${candidate}" \
    bash /opt/heartwood/images/generic/scripts/offline_stack_smoke.sh
fi
