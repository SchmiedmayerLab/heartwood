# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Support ``python -m heartwood.cli``."""

from __future__ import annotations

from heartwood.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
