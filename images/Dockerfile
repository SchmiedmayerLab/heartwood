# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

ARG HEARTWOOD_BASE_IMAGE=ghcr.io/astral-sh/uv:python3.12-trixie-slim
ARG HEARTWOOD_BASE_PLATFORM=$TARGETPLATFORM

FROM --platform=$BUILDPLATFORM node:24-trixie-slim AS webui-build

WORKDIR /src/packages/webui

COPY packages/webui/package.json packages/webui/package-lock.json ./

RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --fund=false

COPY packages/webui ./

RUN npm run build

FROM --platform=${HEARTWOOD_BASE_PLATFORM} ghcr.io/astral-sh/uv:python3.12-trixie-slim AS uv-bin

FROM --platform=${HEARTWOOD_BASE_PLATFORM} ${HEARTWOOD_BASE_IMAGE} AS heartwood-runtime-base

ARG TARGETARCH
ARG HEARTWOOD_BASE_IMAGE
ARG HEARTWOOD_BASE_PLATFORM
ARG HEARTWOOD_PLATFORM=generic
ARG HEARTWOOD_IMAGE_FLAVOR=runtime
ARG HEARTWOOD_GPU_RUNTIME=none
ARG HEARTWOOD_RUNTIME_USER=heartwood
ARG HEARTWOOD_RUNTIME_HOME=/home/heartwood
ARG HEARTWOOD_WORKDIR=/workspace
ARG HEARTWOOD_CREATE_USER=true
ARG HEARTWOOD_UID=10001
ARG HEARTWOOD_GID=10001
ARG HEARTWOOD_JUPYTER_PREFIX=/usr/local
ARG HEARTWOOD_INSTALL_JUPYTER_KERNEL=false
ARG HEARTWOOD_UV_PYTHON_PREFERENCE=system
ARG HEARTWOOD_VERSION=development

LABEL org.opencontainers.image.title="Heartwood Runtime" \
    org.opencontainers.image.description="Heartwood CLI, web UI, notebook bridge, policy and audit controls, verified skills, OpenHands, and optional Heartwood-managed inference." \
    org.opencontainers.image.source="https://github.com/SchmiedmayerLab/heartwood" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${HEARTWOOD_VERSION}" \
    org.heartwood.image.flavor="${HEARTWOOD_IMAGE_FLAVOR}" \
    org.heartwood.platform="${HEARTWOOD_PLATFORM}" \
    org.heartwood.platform.base="${HEARTWOOD_BASE_IMAGE}" \
    org.heartwood.platform.base.platform="${HEARTWOOD_BASE_PLATFORM}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEARTWOOD_GPU_RUNTIME=${HEARTWOOD_GPU_RUNTIME} \
    HEARTWOOD_IMAGE_FLAVOR=${HEARTWOOD_IMAGE_FLAVOR} \
    HEARTWOOD_PLATFORM=${HEARTWOOD_PLATFORM} \
    HEARTWOOD_PLATFORM_HOME=${HEARTWOOD_RUNTIME_HOME} \
    HEARTWOOD_PYTHON=/opt/heartwood/.venv/bin/python \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    OPENHANDS_SUPPRESS_BANNER=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/heartwood/.venv \
    UV_PYTHON_INSTALL_DIR=/opt/heartwood/python \
    UV_PYTHON_PREFERENCE=${HEARTWOOD_UV_PYTHON_PREFERENCE} \
    JUPYTER_PATH="${HEARTWOOD_JUPYTER_PREFIX}/share/jupyter" \
    PATH="/opt/llama.cpp:${PATH}"

USER root

COPY --from=uv-bin /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/
COPY deploy/install-llama-cpp.sh /usr/local/libexec/heartwood-install-llama-cpp

RUN set -eux; \
    if [ "${HEARTWOOD_CREATE_USER}" = "true" ]; then \
      groupadd --system --gid "${HEARTWOOD_GID}" "${HEARTWOOD_RUNTIME_USER}"; \
      useradd --system --uid "${HEARTWOOD_UID}" --gid "${HEARTWOOD_RUNTIME_USER}" \
        --create-home --home-dir "${HEARTWOOD_RUNTIME_HOME}" --shell /bin/bash \
        "${HEARTWOOD_RUNTIME_USER}"; \
    elif [ "${HEARTWOOD_CREATE_USER}" != "false" ]; then \
      echo "HEARTWOOD_CREATE_USER must be true or false" >&2; exit 64; \
    elif ! id "${HEARTWOOD_RUNTIME_USER}" >/dev/null 2>&1; then \
      echo "runtime base does not provide user ${HEARTWOOD_RUNTIME_USER}" >&2; exit 69; \
    fi; \
    runtime_group="$(id -gn "${HEARTWOOD_RUNTIME_USER}")"; \
    mkdir -p /opt/heartwood /opt/heartwood-vllm /opt/llama.cpp "${HEARTWOOD_WORKDIR}"; \
    install -d --mode=0755 \
      --owner="${HEARTWOOD_RUNTIME_USER}" --group="${runtime_group}" \
      "${HEARTWOOD_RUNTIME_HOME}/.cache" \
      "${HEARTWOOD_RUNTIME_HOME}/.cache/flashinfer" \
      "${HEARTWOOD_RUNTIME_HOME}/.cache/huggingface" \
      "${HEARTWOOD_RUNTIME_HOME}/.cache/vllm"; \
    install -d --mode=0700 \
      --owner="${HEARTWOOD_RUNTIME_USER}" --group="${runtime_group}" \
      "${HEARTWOOD_RUNTIME_HOME}/.openhands"; \
    if [ "${HEARTWOOD_CREATE_USER}" = "true" ]; then \
      chown -R "${HEARTWOOD_RUNTIME_USER}:${runtime_group}" \
        "${HEARTWOOD_RUNTIME_HOME}" "${HEARTWOOD_WORKDIR}"; \
    fi; \
    missing_packages=""; \
    for package in ca-certificates curl git jq libgomp1 tmux; do \
      if ! dpkg-query --show --showformat='${db:Status-Abbrev}' "${package}" 2>/dev/null \
        | grep --quiet '^ii '; then \
        missing_packages="${missing_packages} ${package}"; \
      fi; \
    done; \
    if [ -n "${missing_packages}" ]; then \
      apt-get update; \
      DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ${missing_packages}; \
      rm -rf /var/lib/apt/lists/*; \
    fi

RUN set -eux; \
    /usr/local/libexec/heartwood-install-llama-cpp /opt/llama.cpp "${TARGETARCH}"; \
    rm /opt/llama.cpp/.heartwood-runtime.tar.gz; \
    printf '%s\n' /opt/llama.cpp > /etc/ld.so.conf.d/heartwood-llama.conf; \
    ldconfig

WORKDIR /opt/heartwood

COPY images/gpu ./images/gpu

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "${HEARTWOOD_GPU_RUNTIME}" = "vllm" ]; then \
      UV_CACHE_DIR=/root/.cache/uv images/gpu/install_runtime.sh \
        --target /opt/heartwood-vllm --python 3.12 --uv /usr/local/bin/uv; \
    elif [ "${HEARTWOOD_GPU_RUNTIME}" != "none" ]; then \
      echo "unsupported GPU runtime: ${HEARTWOOD_GPU_RUNTIME}" >&2; exit 64; \
    fi

COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY fixtures ./fixtures
COPY skills ./skills
COPY evals ./evals
COPY images ./images
COPY README.md NOTICE ./
COPY documentation ./documentation
COPY --from=webui-build /src/packages/webui/dist ./packages/webui/dist

RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --all-extras --python 3.12

RUN ln -sf /opt/heartwood/.venv/bin/heartwood /usr/local/bin/heartwood \
    && ln -sf /opt/heartwood/.venv/bin/python /usr/local/bin/heartwood-python \
    && if [ "${HEARTWOOD_INSTALL_JUPYTER_KERNEL}" = "true" ]; then \
      test -d "${HEARTWOOD_JUPYTER_PREFIX}"; \
      HOME=/tmp IPYTHONDIR=/tmp/heartwood-ipython \
      /opt/heartwood/.venv/bin/python -m ipykernel install \
        --prefix "${HEARTWOOD_JUPYTER_PREFIX}" \
        --name heartwood \
        --display-name "Python 3 (Heartwood)" \
        --env IPYTHONDIR "/tmp/heartwood-ipython"; \
    elif [ "${HEARTWOOD_INSTALL_JUPYTER_KERNEL}" != "false" ]; then \
      echo "HEARTWOOD_INSTALL_JUPYTER_KERNEL must be true or false" >&2; exit 64; \
    fi

WORKDIR ${HEARTWOOD_WORKDIR}

USER ${HEARTWOOD_RUNTIME_USER}

FROM heartwood-runtime-base AS gpu-ci-validate

RUN /opt/heartwood/images/gpu/verify_runtime.sh

FROM heartwood-runtime-base AS platform-runtime-image

FROM heartwood-runtime-base AS runtime-image

CMD ["heartwood", "--help"]
