# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared session command/event contract for Heartwood.

Every Heartwood interface — the primary CLI, the notebook API, and future UI
surfaces — drives the same session by issuing commands and consuming a single
structured event stream. This package will hold that contract so no interface
owns separate execution semantics.

See ``design/03-architecture.md`` (interaction model) and
``design/09-implementation-plan.md`` (phase 0B) for the contract's role.
"""

from __future__ import annotations

from heartwood.session._contracts import (
    CommandKind,
    EventKind,
    JsonValue,
    SessionCommand,
    SessionEvent,
)

__all__ = [
    "CommandKind",
    "EventKind",
    "JsonValue",
    "SessionCommand",
    "SessionEvent",
    "__version__",
]

__version__ = "0.0.0"
