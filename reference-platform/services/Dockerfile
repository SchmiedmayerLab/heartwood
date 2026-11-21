# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Image of the platform services. Build it from the repository root:
#   docker build --file reference-platform/services/Dockerfile .
# The base is the uv image that images/Dockerfile pins.
FROM ghcr.io/astral-sh/uv:0.13.7-python3.12-trixie-slim@sha256:d4a752dd52f42e0c508bb0b6a912f15922bb32e42f66af30671c3fdc0d759f21

ENV UV_PROJECT_ENVIRONMENT=/opt/heartwood-platform/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# The paths keep the layout of the repository, so the locked path of heartwood-schemas resolves.
WORKDIR /opt/heartwood-platform/reference-platform/services
COPY packages/schemas /opt/heartwood-platform/packages/schemas
COPY reference-platform/services/pyproject.toml reference-platform/services/uv.lock \
     reference-platform/services/.python-version ./
COPY reference-platform/services/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

USER 65532:65532
ENTRYPOINT ["/opt/heartwood-platform/.venv/bin/python", "-I", "-m"]
CMD ["heartwood_platform.control", "/etc/heartwood-control/configuration.json"]
