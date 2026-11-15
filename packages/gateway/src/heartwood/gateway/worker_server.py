# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Register image-owned advisory factories before starting the native worker server."""

from pathlib import Path
from runpy import run_module

from heartwood.gateway._openhands_sdk import register_specialist_catalog
from heartwood.gateway._specialists import load_specialist_catalog


def main() -> None:
    """Keep specialist definitions and factory code in the immutable worker image."""
    register_specialist_catalog(
        load_specialist_catalog(
            Path("/opt/heartwood/agents/verified"),
            Path("/opt/heartwood/vendor/heartwood-skills/skills"),
        )
    )
    run_module("openhands.agent_server", run_name="__main__")


if __name__ == "__main__":
    main()
