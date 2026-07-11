# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

FROM node:24-trixie-slim AS webui-build

WORKDIR /src/packages/webui

COPY packages/webui/package.json packages/webui/package-lock.json ./

RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --fund=false

COPY packages/webui ./

RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

ARG TARGETARCH
ARG HEARTWOOD_IMAGE_FLAVOR=runtime
ARG LLAMA_CPP_VERSION=b9937
ARG LLAMA_CPP_UBUNTU_X64_SHA256=937e10a3fb6c4b1791f943230525e91bea168d1305c1d21079970acb70205df3
ARG LLAMA_CPP_UBUNTU_ARM64_SHA256=c8212588514e33150dcff64fbadbf151d978fd9c4de05d0f66b2267b24310ac4
ARG HEARTWOOD_UID=10001
ARG HEARTWOOD_GID=10001

LABEL org.opencontainers.image.title="Heartwood Generic Runtime" \
    org.opencontainers.image.description="Heartwood CLI, web UI, notebook bridge, policy and audit controls, verified skills, OpenHands, and optional local inference." \
    org.opencontainers.image.source="https://github.com/SchmiedmayerLab/heartwood" \
    org.opencontainers.image.licenses="MIT" \
    org.heartwood.image.flavor="${HEARTWOOD_IMAGE_FLAVOR}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    OPENHANDS_SUPPRESS_BANNER=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HEARTWOOD_LOCAL_RUNTIME_PROFILE=llama-cpp-cpu \
    HEARTWOOD_AGENT_BACKEND=auto \
    HEARTWOOD_SKILLS_DIR=/opt/heartwood/skills/verified \
    HEARTWOOD_MODEL_CATALOG=/opt/heartwood/images/generic/local-runtime/model-catalog.toml \
    HEARTWOOD_MODEL_CACHE=/home/heartwood/.cache/heartwood/models \
    HEARTWOOD_WEB_ROOT=/opt/heartwood/packages/webui/dist \
    LD_LIBRARY_PATH=/opt/llama.cpp \
    PATH="/opt/llama.cpp:/opt/heartwood/.venv/bin:${PATH}"

RUN groupadd --system --gid "${HEARTWOOD_GID}" heartwood \
    && useradd --system --uid "${HEARTWOOD_UID}" --gid heartwood --create-home --home-dir /home/heartwood --shell /bin/bash heartwood \
    && mkdir -p \
      /opt/heartwood \
      /home/heartwood/.cache/heartwood/models \
      /home/heartwood/.local/share/heartwood \
      /home/heartwood/.openhands \
    && chown -R heartwood:heartwood /opt/heartwood /home/heartwood \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      libgomp1 \
      tmux \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) llama_asset="llama-${LLAMA_CPP_VERSION}-bin-ubuntu-x64.tar.gz"; llama_sha256="${LLAMA_CPP_UBUNTU_X64_SHA256}" ;; \
      arm64) llama_asset="llama-${LLAMA_CPP_VERSION}-bin-ubuntu-arm64.tar.gz"; llama_sha256="${LLAMA_CPP_UBUNTU_ARM64_SHA256}" ;; \
      *) echo "unsupported llama.cpp TARGETARCH=${TARGETARCH}" >&2; exit 64 ;; \
    esac; \
    curl --fail --location --show-error --retry 5 --retry-delay 2 --retry-connrefused --connect-timeout 15 --max-time 600 \
      "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_CPP_VERSION}/${llama_asset}" \
      --output /tmp/llama.cpp.tar.gz; \
    echo "${llama_sha256}  /tmp/llama.cpp.tar.gz" | sha256sum --check -; \
    mkdir -p /opt/llama.cpp; \
    tar -xzf /tmp/llama.cpp.tar.gz -C /opt/llama.cpp --strip-components=1; \
    rm /tmp/llama.cpp.tar.gz; \
    chown -R heartwood:heartwood /opt/llama.cpp

ENV HF_HOME=/home/heartwood/.cache/heartwood/models/.huggingface

WORKDIR /opt/heartwood

COPY --chown=heartwood:heartwood pyproject.toml uv.lock ./
COPY --chown=heartwood:heartwood packages ./packages
COPY --chown=heartwood:heartwood fixtures ./fixtures
COPY --chown=heartwood:heartwood skills ./skills
COPY --chown=heartwood:heartwood evals ./evals
COPY --chown=heartwood:heartwood images ./images
COPY --from=webui-build --chown=heartwood:heartwood /src/packages/webui/dist ./packages/webui/dist

USER heartwood

RUN --mount=type=cache,target=/home/heartwood/.cache/uv,uid=${HEARTWOOD_UID},gid=${HEARTWOOD_GID} \
    uv sync --locked --no-dev --all-extras

COPY --chown=heartwood:heartwood README.md ACRONYMS.md ./
COPY --chown=heartwood:heartwood docs ./docs
COPY --chown=heartwood:heartwood design ./design

CMD ["heartwood", "--help"]
