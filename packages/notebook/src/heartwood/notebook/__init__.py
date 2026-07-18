# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Notebook API for Heartwood sessions."""

from __future__ import annotations

from heartwood.gateway import has_authenticated_jupyter_proxy, jupyter_proxy_url
from heartwood.notebook._view_model import (
    ActivityItem,
    ApprovalAction,
    ApprovalControl,
    ChatMessage,
    DatasetProposal,
    ExportAction,
    NotebookSession,
    NotebookViewModel,
    PolicyStatus,
    SkillProposal,
    build_view_model,
)
from heartwood.notebook._widgets import WidgetSpec, build_widget_spec, render_widgets

__all__ = [
    "ActivityItem",
    "ApprovalAction",
    "ApprovalControl",
    "ChatMessage",
    "DatasetProposal",
    "ExportAction",
    "NotebookSession",
    "NotebookViewModel",
    "PolicyStatus",
    "SkillProposal",
    "WidgetSpec",
    "__version__",
    "build_view_model",
    "build_widget_spec",
    "has_authenticated_jupyter_proxy",
    "jupyter_proxy_url",
    "render_widgets",
]

__version__ = "0.2.0-beta.3"
