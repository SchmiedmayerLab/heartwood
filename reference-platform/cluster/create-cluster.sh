#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Creates a kind cluster with Calico and the Agent Sandbox controller from pinned,
# hash-verified upstream manifests, and a local registry NAME-registry for images that
# are built for the cluster. Every kubectl call names the cluster's context, and kind
# writes to the kubeconfig that KUBECONFIG selects.
# Usage: create-cluster.sh NAME

set -euo pipefail

name="${1:?cluster name is required}"
cluster_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
context="kind-${name}"

node_image="kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
calico_url="https://raw.githubusercontent.com/projectcalico/calico/v3.33.1/manifests/calico.yaml"
calico_sha256="3368e62807b949438842e31f46c9b6df741731b4c755d0a622dd062b2cb20495"
agent_sandbox_url="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.3/sandbox.yaml"
agent_sandbox_sha256="725fafdabe6aac202a89dc57f1cfe0e2e92f3164c8c2bd343fffca52f7039d96"
registry_image="registry:3.1.2@sha256:82f6ff4a7f982960c8aa51617fea3aabc485df6e55cce80d22deb1de36f1bc3c"
registry="${name}-registry"

downloads="$(mktemp -d)"
trap 'rm -rf "${downloads}"' EXIT

fetch() {
  local file="${downloads}/$1" expected="$3" actual
  curl --fail --silent --show-error --location --retry 3 --output "${file}" "$2"
  actual="$(openssl dgst -sha256 -r "${file}" | cut -d ' ' -f 1)"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "SHA-256 mismatch for $2: ${actual}" >&2
    exit 1
  fi
}

fetch calico.yaml "${calico_url}" "${calico_sha256}"
fetch agent-sandbox.yaml "${agent_sandbox_url}" "${agent_sandbox_sha256}"

kind create cluster --name "${name}" --image "${node_image}" \
  --config "${cluster_dir}/kind-config.yaml" --wait 0s

# The registry serves only the kind network, where the nodes reach it by name without
# TLS. push-image.sh pushes to it from a container on that network.
docker run --detach --name "${registry}" --network kind "${registry_image}" >/dev/null
for node in $(kind get nodes --name "${name}"); do
  docker exec "${node}" mkdir -p "/etc/containerd/certs.d/${registry}:5000"
  docker exec --interactive "${node}" \
    sh -c "cat > '/etc/containerd/certs.d/${registry}:5000/hosts.toml'" <<EOF
[host."http://${registry}:5000"]
EOF
done

kubectl --context "${context}" apply --filename "${downloads}/calico.yaml"
kubectl --context "${context}" --namespace kube-system rollout status daemonset/calico-node --timeout 300s
kubectl --context "${context}" --namespace kube-system rollout status deployment/calico-kube-controllers --timeout 300s
kubectl --context "${context}" wait --for condition=Ready node --all --timeout 180s

kubectl --context "${context}" apply --filename "${downloads}/agent-sandbox.yaml"
kubectl --context "${context}" --namespace agent-sandbox-system rollout status deployment --timeout 240s
