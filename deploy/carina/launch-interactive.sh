#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

environment_root=""
model_root=""
state_root=""
model_id="heartwood-carina-demo"
while (($#)); do
  case "$1" in
    --environment-root) environment_root="${2:?missing environment root}"; shift 2 ;;
    --model-root) model_root="${2:?missing model root}"; shift 2 ;;
    --state-root) state_root="${2:?missing state root}"; shift 2 ;;
    --model-id) model_id="${2:?missing model id}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
: "${environment_root:?--environment-root is required}"
: "${model_root:?--model-root is required}"
: "${state_root:?--state-root is required}"
: "${SLURM_JOB_ID:?launch Heartwood inside a Slurm compute allocation}"
: "${LOCAL_SCRATCH_JOB:?Carina job-local scratch is unavailable}"

heartwood="${environment_root}/heartwood/bin/heartwood"
if [[ ! -x "${heartwood}" ]]; then
  echo "Heartwood environment is unavailable; run the native installer first" >&2
  exit 69
fi

export HEARTWOOD_PLATFORM=carina
exec "${heartwood}" \
  --workspace "${state_root}/sessions" \
  --session-id carina-demo \
  launch \
  --inside-allocation \
  --environment-root "${environment_root}" \
  --model-root "${model_root}" \
  --state-root "${state_root}" \
  --model-id "${model_id}"
