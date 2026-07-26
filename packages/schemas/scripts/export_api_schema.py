# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Write the canonical public API contract as JSON Schema."""

from __future__ import annotations

import json
import sys

from heartwood.schemas import api_contract_schema


def main() -> None:
    """Write a deterministic schema document to standard output."""
    json.dump(api_contract_schema(), sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
