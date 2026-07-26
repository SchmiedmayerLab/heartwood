# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from typing import Any

import pytest

from packages.gateway.scripts.export_session_projection_schema import (
    _replace_json_value_definition,
)


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({}, "missing the \\$defs object"),
        ({"$defs": []}, "missing the \\$defs object"),
        ({"$defs": {}}, "missing \\$defs.JsonValue"),
    ],
)
def test_projection_schema_export_requires_json_value_definition(
    schema: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        _replace_json_value_definition(schema)


def test_projection_schema_export_replaces_json_value_with_recursive_contract() -> None:
    schema: dict[str, Any] = {"$defs": {"JsonValue": {"type": "string"}}}

    _replace_json_value_definition(schema)

    definition = schema["$defs"]["JsonValue"]
    assert {"type": "null"} in definition["anyOf"]
    assert definition["anyOf"][-1]["additionalProperties"] == {"$ref": "#/$defs/JsonValue"}
