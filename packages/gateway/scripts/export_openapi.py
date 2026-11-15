# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Write the implemented gateway OpenAPI description to standard output."""

from __future__ import annotations

import json
import sys

from heartwood.gateway._openapi import gateway_openapi


def main() -> None:
    """Write deterministic, readable JSON without accessing a running gateway."""
    json.dump(gateway_openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
