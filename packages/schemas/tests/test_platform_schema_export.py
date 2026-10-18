# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify platform schema exports are reproducible and use their owning contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from heartwood.schemas import platform_schema_documents, schema_for
from heartwood.schemas._upstream import platform_contract_notices


def test_platform_schemas_use_existing_records_and_isolate_mutations() -> None:
    documents = platform_schema_documents()
    assert (
        documents["model-connections.schema.json"]["properties"]
        == schema_for("model-connections.v1")["properties"]
    )
    assert (
        documents["policy-profile.schema.json"]["properties"]
        == schema_for("policy-profile.v1")["properties"]
    )
    documents["policy-profile.schema.json"]["properties"].clear()
    assert platform_schema_documents()["policy-profile.schema.json"]["properties"]


def test_platform_export_is_deterministic_and_preserves_existing_files(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "export_platform_schemas.py"
    first, second = tmp_path / "first", tmp_path / "second"
    for destination in (first, second):
        subprocess.run([sys.executable, str(script), str(destination)], check=True)
    expected = platform_schema_documents()
    notices = platform_contract_notices()
    assert sorted(path.name for path in first.iterdir()) == sorted({*expected, *notices})
    for name, schema in expected.items():
        assert json.loads((first / name).read_text()) == schema
        assert (first / name).read_bytes() == (second / name).read_bytes()
    for name, content in notices.items():
        assert (first / name).read_bytes() == content == (second / name).read_bytes()
    sentinel = first / "review-notes.txt"
    sentinel.write_text("preserve this review")
    result = subprocess.run(
        [sys.executable, str(script), str(first)], capture_output=True, check=False
    )
    assert result.returncode != 0
    assert sentinel.read_text() == "preserve this review"


def test_annotation_profile_requires_explicit_hints_without_granting_authority() -> None:
    from jsonschema import Draft202012Validator

    schema = platform_schema_documents()["mcp-tool-annotations.schema.json"]
    validator = Draft202012Validator(schema)
    hints = dict.fromkeys(schema["required"], False)
    assert validator.is_valid(hints)
    assert validator.is_valid({**hints, "title": "Synthetic tool", "approved": True})
    # Unknown properties are not verified or translated into an authorization.
    for hint in hints:
        assert not validator.is_valid({key: value for key, value in hints.items() if key != hint})
        assert not validator.is_valid({**hints, hint: "true"})
