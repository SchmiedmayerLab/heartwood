# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# The generic runtime builds on the pinned uv-bin stage; Terra targets override this image.
ARG HEARTWOOD_BASE_IMAGE=uv-bin
ARG HEARTWOOD_BASE_PLATFORM=$TARGETPLATFORM

FROM --platform=$BUILDPLATFORM node:24-trixie-slim@sha256:8ec5d7557396cfe32d21c3f9c13072355ceab22b584578ca4bb28af31120cffe AS webui-build

WORKDIR /src/packages/webui

COPY packages/webui/package.json packages/webui/package-lock.json packages/webui/.npmrc ./

RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --fund=false

COPY packages/webui ./

RUN npm run build

FROM --platform=${HEARTWOOD_BASE_PLATFORM} ghcr.io/astral-sh/uv:0.13.7-python3.12-trixie-slim@sha256:d4a752dd52f42e0c508bb0b6a912f15922bb32e42f66af30671c3fdc0d759f21 AS uv-bin

# A named build context replaces this placeholder only for explicit qualification builds.
FROM scratch AS worker-qualification-artifacts
COPY images/worker/candidate/sources.json /source-manifest.json

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
ARG HEARTWOOD_DEPENDENCY_PROFILE=locked

LABEL org.opencontainers.image.title="Heartwood Runtime" \
    org.opencontainers.image.description="Heartwood CLI, web UI, notebook bridge, policy and audit controls, verified skills, OpenHands, and optional Heartwood-managed inference." \
    org.opencontainers.image.source="https://github.com/SchmiedmayerLab/heartwood" \
    org.opencontainers.image.licenses="MIT" \
    org.heartwood.image.flavor="${HEARTWOOD_IMAGE_FLAVOR}" \
    org.heartwood.platform="${HEARTWOOD_PLATFORM}" \
    org.heartwood.dependencies="${HEARTWOOD_DEPENDENCY_PROFILE}" \
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
    install -d --mode=0700 \
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
    for package in ca-certificates curl git jq libgomp1 libssl3t64 tmux; do \
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
COPY vendor/heartwood-skills ./vendor/heartwood-skills
COPY agents ./agents
COPY evals ./evals
COPY images ./images
COPY deploy/build-worker-candidate.py ./deploy/build-worker-candidate.py
COPY README.md NOTICE ./
COPY documentation ./documentation
COPY --from=webui-build /src/packages/webui/dist ./packages/webui/dist

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,from=worker-qualification-artifacts,target=/qualification,ro \
    set -eu; \
    export UV_CACHE_DIR=/root/.cache/uv; \
    if [ "${HEARTWOOD_DEPENDENCY_PROFILE}" = "candidate" ]; then \
      test "${HEARTWOOD_PLATFORM}" = "generic"; \
      test "${HEARTWOOD_GPU_RUNTIME}" = "none"; \
      bash images/worker/candidate/runtime.sh \
        install /qualification/sdk /qualification/application /opt/heartwood/.venv; \
      cli_resources="$(/opt/heartwood/.venv/bin/python -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/heartwood/cli/_bundled/packages/webui"; \
      install -d "${cli_resources}"; \
      cp -R packages/webui/dist "${cli_resources}/dist"; \
      install -d /opt/heartwood/qualification; \
      cp /qualification/sdk/build-record.json /qualification/application/application-build-record.json \
        /opt/heartwood/qualification/; \
    elif [ "${HEARTWOOD_DEPENDENCY_PROFILE}" = "locked" ]; then \
      uv run --no-project --python 3.12.15 deploy/build-worker-candidate.py --bootstrap; \
      uv sync --locked --no-dev --all-extras --python 3.12; \
    else \
      echo "unsupported dependency profile: ${HEARTWOOD_DEPENDENCY_PROFILE}" >&2; exit 64; \
    fi

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

FROM heartwood-runtime-base AS heartwood-image-metadata

ARG HEARTWOOD_VERSION=development
ARG HEARTWOOD_REVISION=unknown

LABEL org.opencontainers.image.version="${HEARTWOOD_VERSION}" \
    org.opencontainers.image.revision="${HEARTWOOD_REVISION}"

ENV HEARTWOOD_IMAGE_REVISION=${HEARTWOOD_REVISION}

RUN if [ "${HEARTWOOD_DEPENDENCY_PROFILE}" = "candidate" ]; then \
      /opt/heartwood/.venv/bin/python -I -c 'import json,sys; assert json.load(open("/opt/heartwood/qualification/application-build-record.json"))["commit"] == sys.argv[1], "candidate source revision mismatch"' "${HEARTWOOD_REVISION}"; \
    fi

FROM heartwood-image-metadata AS gpu-ci-validate

RUN /opt/heartwood/images/gpu/verify_runtime.sh

FROM heartwood-image-metadata AS worker-image

USER root
WORKDIR /opt/heartwood

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "${HEARTWOOD_DEPENDENCY_PROFILE}" = "locked" ]; then \
      UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --all-extras --group worker --python 3.12; \
    fi

RUN install -d --mode=0755 /etc/heartwood
COPY --chmod=0444 images/worker/config.json /etc/heartwood/worker.json

WORKDIR /workspace
USER heartwood

RUN test -r /etc/heartwood/worker.json && test ! -w /etc/heartwood/worker.json

ENTRYPOINT ["/opt/heartwood/.venv/bin/python", "-I", "-B", "-u", "/opt/heartwood/images/worker/launch.py"]
CMD []

# The hosted gateway runs one researcher's project behind the platform's proxy. It adds
# kubectl and the OpenShell command-line tool, which the Kubernetes worker adapter runs,
# and the script that prepares its files in the init container. The tool archives carry
# the digests of images/worker/candidate/gateway.json.
FROM registry.k8s.io/kubectl:v1.37.0@sha256:5ed410ebac5dc976cc717098994dcdb29bbbd38f6bd65f582311f5be4ba719cf AS hosted-kubectl

FROM scratch AS hosted-openshell-cli-amd64
ADD --checksum=sha256:3856ac7b6b2e33d6c8ab72a1036444156516e7303da85a9b13f661a2aa63bd2d \
    https://github.com/NVIDIA/OpenShell/releases/download/v0.1.9/openshell-x86_64-unknown-linux-musl.tar.gz /openshell.tar.gz

FROM scratch AS hosted-openshell-cli-arm64
ADD --checksum=sha256:cb923d08703b966f7e0e6b1483ed4ba6ed77bc10812c5502bcc1fb6ee0f6ad59 \
    https://github.com/NVIDIA/OpenShell/releases/download/v0.1.9/openshell-aarch64-unknown-linux-musl.tar.gz /openshell.tar.gz

FROM hosted-openshell-cli-${TARGETARCH} AS hosted-openshell-cli

FROM heartwood-image-metadata AS hosted-gateway-image

USER root
COPY --from=hosted-kubectl --chmod=0555 /bin/kubectl /usr/local/bin/kubectl
RUN --mount=type=bind,from=hosted-openshell-cli,target=/mnt/openshell \
    tar --extract --gzip --file /mnt/openshell/openshell.tar.gz --directory /usr/local/bin openshell \
    && chown root:root /usr/local/bin/openshell && chmod 0555 /usr/local/bin/openshell
COPY --chmod=0555 images/hosted/configure.sh /usr/local/libexec/heartwood-hosted-configure
COPY --chmod=0444 images/hosted/policy.py /usr/local/libexec/heartwood-hosted-policy
RUN install -d --mode=0755 /etc/heartwood /var/lib/heartwood /research

USER heartwood
WORKDIR /research
RUN /usr/local/bin/kubectl version --client >/dev/null && /usr/local/bin/openshell --version >/dev/null

CMD ["heartwood", "gateway", "serve", "--help"]

FROM heartwood-image-metadata AS platform-runtime-image

FROM heartwood-image-metadata AS runtime-image

CMD ["heartwood", "--help"]
