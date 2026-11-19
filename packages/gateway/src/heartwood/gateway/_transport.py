# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Transport requirement for requests that carry a credential."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def require_protected_transport(url: str) -> None:
    """Send credentials only over HTTPS or to a loopback address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError("credentials are sent only over HTTPS or to a loopback address")
