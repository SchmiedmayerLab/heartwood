# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Write the gateway-owned session projection as JSON Schema."""

from __future__ import annotations

import json
import sys

from heartwood.gateway import SessionProjection


def main() -> None:
    """Write a deterministic serialization schema to standard output."""
    schema = SessionProjection.model_json_schema(mode="serialization")
    schema["$defs"]["JsonValue"] = {
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
    json.dump(
        schema,
        sys.stdout,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
