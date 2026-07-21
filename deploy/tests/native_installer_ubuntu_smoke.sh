#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

assets="${1:?asset directory is required}"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
  ca-certificates \
  coreutils \
  curl \
  git \
  libgomp1 \
  tar \
  tmux
uv_version="0.11.29"
uv_archive="/tmp/uv-x86_64-unknown-linux-gnu.tar.gz"
uv_sha256="04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829"
curl --proto '=https' --tlsv1.2 --fail --location --show-error \
  "https://github.com/astral-sh/uv/releases/download/${uv_version}/uv-x86_64-unknown-linux-gnu.tar.gz" \
  --output "${uv_archive}"
printf '%s  %s\n' "${uv_sha256}" "${uv_archive}" | sha256sum --check --strict
mkdir -m 755 /opt/uv
tar -xzf "${uv_archive}" -C /opt/uv --strip-components=1
test -x /opt/uv/uv
export PATH="/opt/uv:${PATH}"
test "$(uv --version)" = "uv ${uv_version} (x86_64-unknown-linux-gnu)"

exec deploy/tests/native_installer_real_smoke.sh "${assets}"
