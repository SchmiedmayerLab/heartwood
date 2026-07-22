#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

df --human-readable /
sudo rm -rf \
  /opt/ghc \
  /usr/local/.ghcup \
  /usr/local/lib/android \
  /usr/share/dotnet \
  /usr/share/swift
df --human-readable /
