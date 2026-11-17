# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Notebook API for Heartwood sessions."""

from __future__ import annotations

from heartwood.notebook._view_model import (
    NotebookSession,
    NotebookViewModel,
    build_view_model,
)
from heartwood.notebook._widgets import (
    WidgetSpec,
    build_policy_spec,
    build_widget_spec,
    render_policy,
    render_widgets,
)

__all__ = [
    "NotebookSession",
    "NotebookViewModel",
    "WidgetSpec",
    "__version__",
    "build_policy_spec",
    "build_view_model",
    "build_widget_spec",
    "render_policy",
    "render_widgets",
]

__version__ = "0.5.1"
