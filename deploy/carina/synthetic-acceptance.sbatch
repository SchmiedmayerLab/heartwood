#!/usr/bin/env bash
#SBATCH --job-name=heartwood-synthetic
#SBATCH --partition=dev
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=02:00:00

set -euo pipefail

: "${HEARTWOOD_ENVIRONMENT_ROOT:?set HEARTWOOD_ENVIRONMENT_ROOT}"
: "${HEARTWOOD_MODEL_ROOT:?set HEARTWOOD_MODEL_ROOT}"
: "${HEARTWOOD_STATE_ROOT:?set HEARTWOOD_STATE_ROOT}"

exec deploy/carina/launch-interactive.sh \
  --environment-root "${HEARTWOOD_ENVIRONMENT_ROOT}" \
  --model-root "${HEARTWOOD_MODEL_ROOT}" \
  --state-root "${HEARTWOOD_STATE_ROOT}"
