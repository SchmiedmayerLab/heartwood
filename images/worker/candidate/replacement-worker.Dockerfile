# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# A distinct image identity with unchanged contents, for a restart that changes the worker image.
ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
LABEL org.heartwood.qualification.replacement="true"
