# syntax=docker/dockerfile:1.7
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

FROM python:3.12-slim

# Pull-request-only surrogate for Terra's notebook base. Published Terra images use
# us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python through docker-bake.hcl.

ENV USER=jupyter \
    HOME=/home/jupyter \
    JUPYTER_PORT=8000 \
    JUPYTER_HOME=/etc/jupyter \
    PATH="/opt/conda/bin:${PATH}"

RUN groupadd --gid 1000 jupyter \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/jupyter --shell /bin/bash jupyter \
    && python -m pip install --no-cache-dir \
      jupyter-events==0.7.0 \
      jupyter_core==5.3.1 \
      jupyter_server==1.24.0 \
      jupyter_server_proxy==4.0.0 \
      markupsafe==2.1.2 \
      nbclassic==0.4.8 \
      notebook==6.5.4 \
      notebook_shim==0.2.3 \
      setuptools==80.9.0 \
      traitlets==5.9.0 \
    && mkdir -p /opt/conda/bin /home/jupyter "${JUPYTER_HOME}/scripts" \
    && ln -s /usr/local/bin/python /opt/conda/bin/python \
    && ln -s /usr/local/bin/python /opt/conda/bin/python3 \
    && ln -s /usr/local/bin/jupyter /opt/conda/bin/jupyter \
    && ln -s /usr/local/bin/jupyter-notebook /opt/conda/bin/jupyter-notebook \
    && printf '%s\n' \
      'import os' \
      '' \
      'c = get_config()' \
      'c.NotebookApp.ip = "0.0.0.0"' \
      'c.NotebookApp.port = 8000' \
      'c.NotebookApp.open_browser = False' \
      'c.NotebookApp.token = ""' \
      'c.NotebookApp.disable_check_xsrf = True' \
      'c.NotebookApp.allow_origin = "*"' \
      'c.NotebookApp.terminado_settings = {"shell_command": ["bash"]}' \
      'if "GOOGLE_PROJECT" in os.environ and "CLUSTER_NAME" in os.environ:' \
      '    fragment = "/" + os.environ["GOOGLE_PROJECT"] + "/" + os.environ["CLUSTER_NAME"] + "/"' \
      'else:' \
      '    fragment = "/"' \
      'c.NotebookApp.base_url = "/notebooks" + fragment' \
      'c.NotebookApp.tornado_settings = {"static_url_prefix": "/notebooks" + fragment + "static/"}' \
      > "${JUPYTER_HOME}/jupyter_notebook_config.py" \
    && printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -e' \
      'umask 002' \
      'NOTEBOOKS_DIR=${1:-${HOME}}' \
      'JUPYTER_BASE="/opt/conda/bin/python3 /opt/conda/bin/jupyter-notebook"' \
      'JUPYTER_CMD="$JUPYTER_BASE &> ${NOTEBOOKS_DIR}/jupyter.log"' \
      'echo $JUPYTER_CMD' \
      'eval $JUPYTER_CMD' \
      > "${JUPYTER_HOME}/scripts/run-jupyter.sh" \
    && chmod +x "${JUPYTER_HOME}/scripts/run-jupyter.sh" \
    && chown -R jupyter:jupyter /home/jupyter /opt/conda "${JUPYTER_HOME}"

EXPOSE 8000

WORKDIR /home/jupyter

ENTRYPOINT ["/opt/conda/bin/jupyter", "notebook"]
