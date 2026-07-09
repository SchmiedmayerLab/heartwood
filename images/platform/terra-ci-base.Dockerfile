# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

FROM python:3.12-slim

ENV USER=jupyter \
    HOME=/home/jupyter

RUN groupadd --gid 1000 jupyter \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/jupyter --shell /bin/bash jupyter \
    && mkdir -p /opt/conda/bin /home/jupyter \
    && ln -s /usr/local/bin/python /opt/conda/bin/python \
    && printf '%s\n' '#!/bin/sh' 'exec python -m http.server 8888 "$@"' > /opt/conda/bin/jupyter \
    && chmod +x /opt/conda/bin/jupyter \
    && chown -R jupyter:jupyter /home/jupyter /opt/conda

WORKDIR /home/jupyter

ENTRYPOINT ["/opt/conda/bin/jupyter"]
CMD ["notebook"]
