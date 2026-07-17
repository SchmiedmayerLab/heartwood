# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

ARG HEARTWOOD_PLATFORM_BASE_IMAGE=us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6
ARG HEARTWOOD_PLATFORM_BASE_PLATFORM=linux/amd64

FROM --platform=$BUILDPLATFORM node:24-trixie-slim AS webui-build

WORKDIR /src/packages/webui

COPY packages/webui/package.json packages/webui/package-lock.json ./

RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --fund=false

COPY packages/webui ./

RUN npm run build

FROM --platform=${HEARTWOOD_PLATFORM_BASE_PLATFORM} ghcr.io/astral-sh/uv:python3.12-trixie-slim AS uv-bin

FROM --platform=${HEARTWOOD_PLATFORM_BASE_PLATFORM} ${HEARTWOOD_PLATFORM_BASE_IMAGE} AS heartwood-platform-base

ARG HEARTWOOD_PLATFORM_BASE_IMAGE
ARG HEARTWOOD_PLATFORM_BASE_PLATFORM
ARG HEARTWOOD_PLATFORM=terra
ARG HEARTWOOD_IMAGE_FLAVOR=terra-runtime
ARG HEARTWOOD_GPU_RUNTIME=none
ARG HEARTWOOD_RUNTIME_ARCH=amd64
ARG HEARTWOOD_PLATFORM_HOME=/home/jupyter
ARG HEARTWOOD_PLATFORM_USER=jupyter
ARG HEARTWOOD_JUPYTER_PREFIX=/opt/conda
ARG LLAMA_CPP_VERSION=b9937
ARG LLAMA_CPP_UBUNTU_X64_SHA256=937e10a3fb6c4b1791f943230525e91bea168d1305c1d21079970acb70205df3
ARG LLAMA_CPP_UBUNTU_ARM64_SHA256=c8212588514e33150dcff64fbadbf151d978fd9c4de05d0f66b2267b24310ac4

LABEL org.opencontainers.image.title="Heartwood Platform Runtime" \
    org.opencontainers.image.description="Platform-derived Heartwood runtime with CLI, web UI, notebook bridge, policy and audit controls, verified skills, OpenHands, and optional local inference." \
    org.opencontainers.image.source="https://github.com/SchmiedmayerLab/heartwood" \
    org.opencontainers.image.licenses="MIT" \
    org.heartwood.image.flavor="${HEARTWOOD_IMAGE_FLAVOR}" \
    org.heartwood.platform="${HEARTWOOD_PLATFORM}" \
    org.heartwood.platform.base="${HEARTWOOD_PLATFORM_BASE_IMAGE}" \
    org.heartwood.platform.base.platform="${HEARTWOOD_PLATFORM_BASE_PLATFORM}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEARTWOOD_GPU_RUNTIME=${HEARTWOOD_GPU_RUNTIME} \
    HEARTWOOD_IMAGE_FLAVOR=${HEARTWOOD_IMAGE_FLAVOR} \
    HEARTWOOD_PLATFORM=${HEARTWOOD_PLATFORM} \
    HEARTWOOD_PLATFORM_HOME=${HEARTWOOD_PLATFORM_HOME} \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    OPENHANDS_SUPPRESS_BANNER=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/heartwood/.venv \
    UV_PYTHON_INSTALL_DIR=/opt/heartwood/python \
    UV_PYTHON_PREFERENCE=managed \
    HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python \
    JUPYTER_PATH="${HEARTWOOD_JUPYTER_PREFIX}/share/jupyter" \
    LD_LIBRARY_PATH="/opt/llama.cpp:${LD_LIBRARY_PATH}" \
    PATH="/opt/llama.cpp:${PATH}"

USER root

COPY --from=uv-bin /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      libgomp1 \
      tmux \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${HEARTWOOD_RUNTIME_ARCH}" in \
      amd64) llama_asset="llama-${LLAMA_CPP_VERSION}-bin-ubuntu-x64.tar.gz"; llama_sha256="${LLAMA_CPP_UBUNTU_X64_SHA256}" ;; \
      arm64) llama_asset="llama-${LLAMA_CPP_VERSION}-bin-ubuntu-arm64.tar.gz"; llama_sha256="${LLAMA_CPP_UBUNTU_ARM64_SHA256}" ;; \
      *) echo "unsupported llama.cpp HEARTWOOD_RUNTIME_ARCH=${HEARTWOOD_RUNTIME_ARCH}" >&2; exit 64 ;; \
    esac; \
    curl --fail --location --show-error --retry 5 --retry-delay 2 --retry-connrefused --connect-timeout 15 --max-time 600 \
      "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_CPP_VERSION}/${llama_asset}" \
      --output /tmp/llama.cpp.tar.gz; \
    echo "${llama_sha256}  /tmp/llama.cpp.tar.gz" | sha256sum --check -; \
    mkdir -p /opt/llama.cpp; \
    tar -xzf /tmp/llama.cpp.tar.gz -C /opt/llama.cpp --strip-components=1; \
    rm /tmp/llama.cpp.tar.gz

WORKDIR /opt/heartwood

COPY images/gpu/vllm-requirements.txt ./images/gpu/vllm-requirements.txt

RUN --mount=type=cache,target=/root/.cache/uv \
    export UV_CACHE_DIR=/root/.cache/uv; \
    if [ "${HEARTWOOD_GPU_RUNTIME}" = "vllm" ]; then \
      uv venv /opt/heartwood-vllm --python 3.12 \
      && uv pip sync --require-hashes --python /opt/heartwood-vllm/bin/python images/gpu/vllm-requirements.txt; \
    elif [ "${HEARTWOOD_GPU_RUNTIME}" != "none" ]; then \
      echo "unsupported GPU runtime: ${HEARTWOOD_GPU_RUNTIME}" >&2; exit 64; \
    fi

COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY fixtures ./fixtures
COPY skills ./skills
COPY evals ./evals
COPY images ./images

RUN if [ "${HEARTWOOD_GPU_RUNTIME}" = "vllm" ]; then \
      install -m 0444 images/gpu/heartwood_vllm.py /opt/heartwood-vllm/bin/heartwood_vllm.py; \
      install -m 0444 images/gpu/sitecustomize.py /opt/heartwood-vllm/bin/sitecustomize.py; \
      install -m 0555 images/gpu/heartwood-vllm /opt/heartwood-vllm/bin/heartwood-vllm; \
    fi

COPY README.md ACRONYMS.md ./
COPY docs ./docs
COPY design ./design
COPY --from=webui-build /src/packages/webui/dist ./packages/webui/dist

RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --all-extras --python 3.12

RUN ln -sf /opt/heartwood/.venv/bin/heartwood /usr/local/bin/heartwood \
    && ln -sf /opt/heartwood/.venv/bin/python /usr/local/bin/heartwood-python

RUN if [ -d "${HEARTWOOD_JUPYTER_PREFIX}" ]; then \
      HOME=/tmp IPYTHONDIR=/tmp/heartwood-ipython \
      /opt/heartwood/.venv/bin/python -m ipykernel install \
        --prefix "${HEARTWOOD_JUPYTER_PREFIX}" \
        --name heartwood \
        --display-name "Python 3 (Heartwood)"; \
    fi

WORKDIR ${HEARTWOOD_PLATFORM_HOME}

USER ${HEARTWOOD_PLATFORM_USER}

FROM heartwood-platform-base AS gpu-ci-validate

RUN /opt/heartwood/images/gpu/verify_runtime.sh

FROM heartwood-platform-base AS platform-runtime-image
