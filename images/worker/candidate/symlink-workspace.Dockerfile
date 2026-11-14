# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
USER root
RUN ln -s /workspace /workspace-link
WORKDIR /workspace-link
USER heartwood
