#!/bin/sh
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# Prepares the files of a hosted Heartwood gateway in its init container. It copies the
# session's configuration Secret into owner-only files, registers the session's
# OpenShell gateway with its client certificate, and creates the research and state
# directories on the server's volume. Only this container mounts the Secret.

set -eu
umask 077

source=/etc/heartwood/source
work=/etc/heartwood/work
home=/var/lib/heartwood-home

# A restarted init container starts again from empty directories.
rm -rf "${work}/deployment" "${work}/native"
mkdir -p "${work}/deployment" "${work}/native" "${home}/research" "${home}/state"
for name in deployment.toml platform-ca.crt actor-key kubeconfig worker-policy.json \
  launch-context.json model-discovery-key; do
  cp "${source}/${name}" "${work}/deployment/${name}"
done
cp "${source}/keycloak-client-secret" "${work}/deployment/client-secret"

mtls="${work}/native/openshell/gateways/openshell/mtls"
mkdir -p "${mtls}"
for name in ca.crt tls.crt tls.key; do
  cp "${source}/openshell-${name}" "${mtls}/${name}"
done
endpoint="$(/opt/heartwood/.venv/bin/python -I -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["openshell_endpoint"])' \
  "${source}/launch.json")"
XDG_CONFIG_HOME="${work}/native" /usr/local/bin/openshell gateway add "${endpoint}" \
  --name openshell --local >/dev/null
# The digest of the workers' effective sandbox policy is particular to this session's
# OpenShell gateway, so it is learned there and written into the manifest.
/opt/heartwood/.venv/bin/python -I -B /usr/local/libexec/heartwood-hosted-policy \
  "${work}/deployment" "${work}/native"

find "${work}" -mindepth 1 -type d -exec chmod 0700 {} +
find "${work}" -mindepth 1 -type f -exec chmod 0600 {} +
chmod 0700 "${home}/research" "${home}/state"
