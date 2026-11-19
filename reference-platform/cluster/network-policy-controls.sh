#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Shows that the cluster enforces NetworkPolicy.
# A reachable-connection control must connect, and two blocked-connection controls, one
# denied by an ingress policy and one by an egress policy, must time out. The reachable
# control then runs again, and after the policies are deleted both blocked pods must
# connect, which shows that the blocks came from the policies.
# The script writes network-policy-controls.json to OUTPUT and exits non-zero unless
# every control has its expected result.
# Usage: network-policy-controls.sh NAME OUTPUT

set -euo pipefail

name="${1:?cluster name is required}"
output="${2:?output directory is required}"
cluster_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
context="kind-${name}"
namespace="hw-network-controls"
attempts=20
# Consecutive timed-out connections that count as blocked, so one lost packet on a
# cluster that does not enforce policy cannot pass as a block. Calico drops denied
# packets, so only a timeout counts; any other failure is inconclusive.
blocked_after=3

cluster() { kubectl --context "${context}" "$@"; }

mkdir -p "${output}"
cluster apply --filename "${cluster_dir}/network-policy-controls.yaml"
trap 'cluster delete namespace "${namespace}" --wait=false >/dev/null' EXIT
cluster --namespace "${namespace}" wait --for condition=Ready pod --all --timeout 180s
target="$(cluster --namespace "${namespace}" get pod target --output 'jsonpath={.status.podIP}'):8080"

# Prints "connected", "timeout" or "error".
probe() {
  local result
  if result="$(cluster --namespace "${namespace}" exec "$1" -- \
    /agnhost connect --timeout=3s "${target}" 2>&1)"; then
    echo connected
  elif [[ "${result}" == TIMEOUT* ]]; then
    echo timeout
  else
    echo error
  fi
}

# Prints "reachable" or "blocked" and the number of attempts used.
observe() {
  local pod="$1" expected="$2" attempt failures=0
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    case "$(probe "${pod}")" in
      connected)
        failures=0
        if [[ "${expected}" == reachable ]]; then
          echo "reachable ${attempt}"
          return
        fi
        ;;
      timeout)
        failures=$((failures + 1))
        if [[ "${expected}" == blocked && "${failures}" -ge "${blocked_after}" ]]; then
          echo "blocked ${attempt}"
          return
        fi
        ;;
      *) failures=0 ;;
    esac
    sleep 2
  done
  if [[ "${expected}" == reachable ]]; then
    echo "blocked ${attempts}"
  else
    echo "reachable ${attempts}"
  fi
}

passed=true
results=()
control() {
  local phase="$1" expected="$2" pod="$3" observed used
  read -r observed used < <(observe "${pod}" "${expected}")
  echo "${phase}: ${pod}: expected ${expected}, observed ${observed} after ${used} attempt(s)"
  [[ "${observed}" == "${expected}" ]] || passed=false
  results+=("$(jq --null-input --compact-output --arg phase "${phase}" --arg pod "${pod}" \
    --arg expected "${expected}" --arg observed "${observed}" --argjson attempts "${used}" \
    '{phase: $phase, pod: $pod, expected: $expected, observed: $observed, attempts: $attempts}')")
}

# The reachable control runs first: it shows that the target serves and that the
# policies are in place before the blocked controls are judged.
control enforced reachable client
control enforced blocked outsider
control enforced blocked egress-denied
control enforced reachable client
cluster --namespace "${namespace}" delete networkpolicy --all --wait
control "policies removed" reachable outsider
control "policies removed" reachable egress-denied

printf '%s\n' "${results[@]}" | jq --slurp \
  --arg cluster "${name}" \
  --arg kubernetes "$(cluster version --output json | jq --raw-output .serverVersion.gitVersion)" \
  --arg cni "$(cluster --namespace kube-system get daemonset calico-node \
    --output 'jsonpath={.spec.template.spec.containers[0].image}')" \
  --argjson passed "${passed}" \
  '{cluster: $cluster, kubernetes: $kubernetes, cni: $cni, passed: $passed, controls: .}' \
  >"${output}/network-policy-controls.json"

if [[ "${passed}" != true ]]; then
  echo "A network policy control did not have its expected result." >&2
  exit 1
fi
