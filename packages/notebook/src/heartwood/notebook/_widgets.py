# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Optional widget rendering for notebook view models."""

from __future__ import annotations

import html
import importlib
import json
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol, cast

from heartwood.notebook._view_model import NotebookViewModel


@dataclass(frozen=True, slots=True)
class WidgetSpec:
    """Deterministic widget section specification used when widgets are unavailable."""

    title: str
    items: tuple[str, ...]


class _WidgetFactory(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object:
        """Create one widget object."""


def build_widget_spec(view_model: NotebookViewModel) -> tuple[WidgetSpec, ...]:
    """Build deterministic widget section specifications."""
    return (
        WidgetSpec(
            "Chat",
            tuple(f"{message.role}: {message.content}" for message in view_model.chat),
        ),
        WidgetSpec(
            "Activity",
            tuple(
                f"{item.sequence:03d} {item.label}: {item.detail}" for item in view_model.activity
            ),
        ),
        WidgetSpec(
            "Datasets",
            tuple(
                f"{proposal.dataset_type} ({proposal.confidence:.2f})"
                for proposal in view_model.dataset_proposals
            ),
        ),
        WidgetSpec(
            "Skills",
            tuple(
                f"{proposal.target_id}: {proposal.status}"
                for proposal in view_model.skill_proposals
            ),
        ),
        WidgetSpec(
            "Approvals",
            _approval_items(view_model),
        ),
        WidgetSpec(
            "Policy",
            tuple(
                f"{status.decision} {status.endpoint}: {status.reason}"
                for status in view_model.policy_status
            ),
        ),
        WidgetSpec(
            "Exports",
            tuple(f"{action.label}: {action.path}" for action in view_model.export_actions),
        ),
    )


def _approval_items(view_model: NotebookViewModel) -> tuple[str, ...]:
    items: list[str] = []
    for control in view_model.approval_controls:
        items.append(f"{control.label}: {control.decision or 'pending'}")
        items.extend(
            f"{index}. {action.summary} ({action.tool_name}, {action.risk} risk)"
            + (
                f"\nArguments:\n{json.dumps(action.arguments, indent=2, sort_keys=True)}"
                if action.arguments
                else ""
            )
            for index, action in enumerate(control.actions, 1)
        )
    return tuple(items)


def render_widgets(view_model: NotebookViewModel) -> object:
    """Render ``ipywidgets`` if installed, otherwise return widget specifications."""
    widgets = _load_widgets()
    if widgets is None:
        return build_widget_spec(view_model)
    html_widget = _factory(widgets, "HTML")
    vbox = _factory(widgets, "VBox")
    sections = [
        html_widget(value=_section_html(spec.title, spec.items))
        for spec in build_widget_spec(view_model)
    ]
    return vbox(sections)


def _load_widgets() -> ModuleType | None:
    try:
        return importlib.import_module("ipywidgets")
    except ImportError:
        return None


def _factory(module: ModuleType, name: str) -> _WidgetFactory:
    candidate = getattr(module, name)
    if not callable(candidate):
        msg = f"ipywidgets.{name} is not callable"
        raise TypeError(msg)
    return cast(_WidgetFactory, candidate)


def _section_html(title: str, items: tuple[str, ...]) -> str:
    escaped_title = html.escape(title)
    if not items:
        return f"<section><h3>{escaped_title}</h3><p>None</p></section>"
    rendered_items = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f"<section><h3>{escaped_title}</h3><ul>{rendered_items}</ul></section>"
