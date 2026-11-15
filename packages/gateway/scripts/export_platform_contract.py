# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Export the versioned platform contract to a new directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from heartwood.gateway._openapi import gateway_openapi
from heartwood.schemas._platform_contract import (
    contract_json_bytes,
    platform_artifact_bytes,
    platform_contract_index,
    platform_schema_documents,
)
from heartwood.schemas._upstream import platform_contract_notices


def main() -> None:
    """Compose existing owners without accessing a live gateway or replacing files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for the contract bundle")
    arguments = parser.parse_args()
    documents = platform_schema_documents()
    documents["gateway.openapi.json"] = gateway_openapi()
    index = platform_contract_index(documents)
    arguments.output.mkdir(parents=True, exist_ok=False)
    for name, document in documents.items():
        (arguments.output / name).write_bytes(platform_artifact_bytes(name, document))
    (arguments.output / "contract.json").write_bytes(contract_json_bytes(index))
    for name, content in platform_contract_notices().items():
        (arguments.output / name).write_bytes(content)


if __name__ == "__main__":
    main()
