# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Smoke test: the session package imports and exposes a version string."""

from __future__ import annotations

import heartwood.session


def test_version_is_nonempty_string() -> None:
    """The package advertises a non-empty version string."""
    assert isinstance(heartwood.session.__version__, str)
    assert heartwood.session.__version__
