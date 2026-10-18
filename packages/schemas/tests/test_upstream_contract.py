# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Upstream definitions remain pinned, self-contained, and separate from owned schemas."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from heartwood.schemas import _upstream
from heartwood.schemas._platform_contract import platform_artifact_bytes


def test_upstream_schema_keeps_exact_source_bytes_and_local_references() -> None:
    source = _upstream.mcp_tasks_schema_bytes()
    assert hashlib.sha256(source).hexdigest() == _upstream.MCP_TASKS_SCHEMA_SHA256
    schema = _upstream.mcp_tasks_schema()
    Draft202012Validator.check_schema(schema)
    assert platform_artifact_bytes("mcp-tasks.schema.json", schema) == source
    pending: list[object] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if "$ref" in item:
                assert item["$ref"].startswith("#/$defs/")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def test_upstream_exports_cannot_silently_fork_the_source() -> None:
    schema = _upstream.mcp_tasks_schema()
    schema["$defs"]["CreateTaskResult"].clear()
    assert _upstream.mcp_tasks_schema()["$defs"]["CreateTaskResult"]
    with pytest.raises(ValueError, match="must not be modified"):
        platform_artifact_bytes("mcp-tasks.schema.json", schema)


def test_modified_packaged_source_fails_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "upstream").mkdir()
    (tmp_path / "upstream" / "mcp-tasks.schema.json").write_text("{}")
    monkeypatch.setattr(_upstream, "files", lambda _: tmp_path)
    with pytest.raises(RuntimeError, match="checksum"):
        _upstream.mcp_tasks_schema()
