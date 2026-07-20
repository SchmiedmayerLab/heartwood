# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Authenticated Jupyter proxy routes shared by every interaction surface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote


def has_authenticated_jupyter_proxy(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the environment identifies an authenticated Jupyter proxy."""
    active_env = os.environ if env is None else env
    service_prefix = active_env.get("JUPYTERHUB_SERVICE_PREFIX", "").strip()
    google_project = active_env.get("GOOGLE_PROJECT", "").strip()
    cluster_name = active_env.get("CLUSTER_NAME", "").strip()
    return bool(service_prefix or (google_project and cluster_name))


def jupyter_proxy_url(*, port: int, env: Mapping[str, str] | None = None) -> str | None:
    """Build an authenticated proxy route only from verified environment evidence."""
    active_env = os.environ if env is None else env
    service_prefix = active_env.get("JUPYTERHUB_SERVICE_PREFIX", "").strip()
    if service_prefix:
        normalized = service_prefix if service_prefix.startswith("/") else f"/{service_prefix}"
        return f"{normalized.rstrip('/')}/proxy/{port}/"

    google_project = active_env.get("GOOGLE_PROJECT", "").strip()
    cluster_name = active_env.get("CLUSTER_NAME", "").strip()
    if google_project and cluster_name:
        project = quote(google_project, safe="")
        cluster = quote(cluster_name, safe="")
        return f"/proxy/{project}/{cluster}/jupyter/proxy/{port}/"

    return None
