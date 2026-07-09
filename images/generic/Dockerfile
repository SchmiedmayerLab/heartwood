# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /opt/heartwood

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/heartwood/.venv/bin:${PATH}"

COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY fixtures ./fixtures
COPY skills ./skills
COPY evals ./evals
COPY images ./images

RUN useradd --create-home --shell /bin/bash heartwood \
    && chown -R heartwood:heartwood /opt/heartwood

USER heartwood

RUN uv sync --locked --no-dev --all-extras

CMD ["heartwood", "--help"]
