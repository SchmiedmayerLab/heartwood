# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reviewable contract exports keep versions, source owners, and byte digests together."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from heartwood.schemas._platform_contract import (
    PLATFORM_CONTRACT_VERSION,
    platform_schema_documents,
)
from heartwood.schemas._upstream import MCP_TASKS_SCHEMA_SHA256, platform_contract_notices


def test_complete_bundle_exports_valid_schemas_and_exact_digests(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "export_platform_contract.py"
    destination = tmp_path / "bundle"
    subprocess.run([sys.executable, str(script), str(destination)], check=True)
    index = json.loads((destination / "contract.json").read_text())
    assert index["contract_version"] == PLATFORM_CONTRACT_VERSION
    artifacts = {item["path"]: item for item in index["artifacts"]}
    assert set(artifacts) == {*platform_schema_documents(), "gateway.openapi.json"}
    for name, entry in artifacts.items():
        content = (destination / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        document = json.loads(content)
        if name.endswith(".schema.json"):
            Draft202012Validator.check_schema(document)
    assert artifacts["gateway.openapi.json"]["maturity"] == "current"
    assert artifacts["platform-estimate.schema.json"]["maturity"] == "draft"
    assert artifacts["operation-set.schema.json"]["maturity"] == "draft"
    assert artifacts["mcp-tasks.schema.json"]["maturity"] == "upstream"
    assert artifacts["mcp-tasks.schema.json"]["sha256"] == MCP_TASKS_SCHEMA_SHA256
    notices = {item["path"]: item for item in index["notices"]}
    assert set(notices) == set(platform_contract_notices())
    for name, content in platform_contract_notices().items():
        assert (destination / name).read_bytes() == content
        assert notices[name]["sha256"] == hashlib.sha256(content).hexdigest()


def test_bundle_determinism_and_existing_directory_protection(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "export_platform_contract.py"
    first, second = tmp_path / "first", tmp_path / "second"
    for destination in (first, second):
        subprocess.run([sys.executable, str(script), str(destination)], check=True)
    first_contents = {p.name: p.read_bytes() for p in first.iterdir()}
    assert first_contents == {p.name: p.read_bytes() for p in second.iterdir()}
    retry = subprocess.run(
        [sys.executable, str(script), str(first)], capture_output=True, check=False
    )
    assert retry.returncode != 0
    assert first_contents == {p.name: p.read_bytes() for p in first.iterdir()}
