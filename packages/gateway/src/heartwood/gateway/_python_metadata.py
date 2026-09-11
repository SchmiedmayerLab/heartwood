# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Standard-library-only probe, also executable inside an isolated analysis environment."""

import json
import platform
from importlib.metadata import distributions


def python_metadata() -> dict[str, object]:
    """Collect version metadata without requiring Heartwood in the target interpreter."""
    return {
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "packages": tuple(
            sorted((item.metadata["Name"], item.version) for item in distributions())
        ),
    }


if __name__ == "__main__":
    print(json.dumps(python_metadata()))
