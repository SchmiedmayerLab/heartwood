# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Write the gateway-owned session projection as JSON Schema."""

from __future__ import annotations

import json
import sys
from typing import Any

from heartwood.gateway import SessionProjection


def _replace_json_value_definition(schema: dict[str, Any]) -> None:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise RuntimeError("session projection schema is missing the $defs object")
    if "JsonValue" not in definitions:
        raise RuntimeError("session projection schema is missing $defs.JsonValue")
    definitions["JsonValue"] = {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
            {
                "type": "array",
                "items": {"$ref": "#/$defs/JsonValue"},
            },
            {
                "type": "object",
                "additionalProperties": {"$ref": "#/$defs/JsonValue"},
            },
        ]
    }


def main() -> None:
    """Write a deterministic serialization schema to standard output."""
    schema = SessionProjection.model_json_schema(mode="serialization")
    _replace_json_value_definition(schema)
    json.dump(
        schema,
        sys.stdout,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
