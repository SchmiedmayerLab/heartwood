# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Image of the platform services. Build it from the repository root:
#   docker build --file reference-platform/services/Dockerfile .
# The base is the uv image that images/Dockerfile pins.
# The control service manages the model route provider in session OpenShell gateways
# with the OpenShell command-line tool of images/worker/candidate/gateway.json.
FROM scratch AS openshell-cli-amd64
ADD --checksum=sha256:3856ac7b6b2e33d6c8ab72a1036444156516e7303da85a9b13f661a2aa63bd2d \
    https://github.com/NVIDIA/OpenShell/releases/download/v0.1.9/openshell-x86_64-unknown-linux-musl.tar.gz /openshell.tar.gz

FROM scratch AS openshell-cli-arm64
ADD --checksum=sha256:cb923d08703b966f7e0e6b1483ed4ba6ed77bc10812c5502bcc1fb6ee0f6ad59 \
    https://github.com/NVIDIA/OpenShell/releases/download/v0.1.9/openshell-aarch64-unknown-linux-musl.tar.gz /openshell.tar.gz

FROM openshell-cli-${TARGETARCH} AS openshell-cli

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

RUN --mount=type=bind,from=openshell-cli,target=/mnt/openshell \
    tar --extract --gzip --file /mnt/openshell/openshell.tar.gz --directory /usr/local/bin openshell \
    && chown root:root /usr/local/bin/openshell && chmod 0555 /usr/local/bin/openshell \
    && /usr/local/bin/openshell --version >/dev/null

USER 65532:65532
ENTRYPOINT ["/opt/heartwood-platform/.venv/bin/python", "-I", "-m"]
CMD ["heartwood_platform.control", "/etc/heartwood-control/configuration.json"]
