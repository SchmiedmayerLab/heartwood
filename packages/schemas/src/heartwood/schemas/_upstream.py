# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Pinned upstream resources, preserved separately from Heartwood contract definitions."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any

MCP_TASKS_VERSION = "2026-07-28"
MCP_TASKS_REVISION = "0d0a6bd4c258b35caa3c810a1dd506cf105b1501"
MCP_TASKS_SCHEMA_SHA256 = "bf30afb7ac251e3e22c037b7a685f60ef6603031b5484c0d08b1fa0bbe86d460"
MCP_TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


def mcp_tasks_schema_bytes() -> bytes:
    """Return the unchanged source bytes, failing closed on a modified resource."""
    content = files("heartwood.schemas").joinpath("upstream", "mcp-tasks.schema.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != MCP_TASKS_SCHEMA_SHA256:
        raise RuntimeError("Pinned MCP Tasks schema checksum does not match")
    return content


def mcp_tasks_schema() -> dict[str, Any]:
    """Return a fresh upstream schema with only local references; no network is used."""
    schema: dict[str, Any] = json.loads(mcp_tasks_schema_bytes())
    return schema


def platform_contract_notices() -> dict[str, bytes]:
    """Return the license and source notice that accompany the upstream artifact."""
    root = files("heartwood.schemas").joinpath("upstream")
    return {
        name: root.joinpath(name).read_bytes()
        for name in ("mcp-tasks.LICENSE.txt", "mcp-tasks.SOURCE.txt")
    }
