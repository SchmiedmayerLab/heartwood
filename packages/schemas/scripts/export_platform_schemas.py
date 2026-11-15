# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Write independently usable platform JSON Schemas to a new output directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from heartwood.schemas import platform_schema_documents
from heartwood.schemas._platform_contract import platform_artifact_bytes
from heartwood.schemas._upstream import platform_contract_notices


def main() -> None:
    """Export deterministically without overwriting an existing artifact directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for the schema files")
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=False)
    for name, schema in platform_schema_documents().items():
        (arguments.output / name).write_bytes(platform_artifact_bytes(name, schema))
    for name, content in platform_contract_notices().items():
        (arguments.output / name).write_bytes(content)


if __name__ == "__main__":
    main()
