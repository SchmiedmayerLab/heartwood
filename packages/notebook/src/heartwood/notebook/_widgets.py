# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Optional widget rendering for the shared session projection."""

from __future__ import annotations

import html
import importlib
import json
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol, cast

from heartwood.gateway import action_risk_label, action_tool_label
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
            "Conversation",
            _conversation_items(view_model),
        ),
        WidgetSpec(
            "Activity",
            tuple(
                f"{item.sequence:03d} {item.label}: {item.detail}" for item in view_model.activity
            ),
        ),
        WidgetSpec(
            "Action Review",
            _approval_items(view_model),
        ),
        WidgetSpec(
            "Tasks",
            tuple(f"{task.status}: {task.title}" for task in view_model.task_plan),
        ),
        WidgetSpec(
            "Runtime",
            _runtime_items(view_model),
        ),
        WidgetSpec(
            "Specialists",
            tuple(
                f"{agent.agent_name}: {agent.status} ({agent.invocation_id})"
                for agent in view_model.subagents
            ),
        ),
    )


def _conversation_items(view_model: NotebookViewModel) -> tuple[str, ...]:
    items = [
        f"{message.label}: {message.content}" + (f"\n{message.detail}" if message.detail else "")
        for message in view_model.conversation
    ]
    if view_model.streaming_text:
        items.append(f"Agent (working): {view_model.streaming_text}")
    return tuple(items)


def _approval_items(view_model: NotebookViewModel) -> tuple[str, ...]:
    group = view_model.pending_approval
    if group is None:
        return ()
    action_label = "action" if len(group.actions) == 1 else "actions"
    items = [(f"Review action set {group.group_id} ({len(group.actions)} {action_label}): pending")]
    items.extend(
        f"{index}. {action.summary or action.tool_name}"
        f"\n{action_tool_label(action.tool_name)} · "
        f"{action_risk_label(action.risk or 'unknown')}"
        + (
            f"\nArguments:\n{json.dumps(action.arguments, indent=2, sort_keys=True)}"
            if action.arguments
            else ""
        )
        for index, action in enumerate(group.actions, 1)
    )
    return tuple(items)


def _runtime_items(view_model: NotebookViewModel) -> tuple[str, ...]:
    items = [f"Lifecycle: {view_model.lifecycle.status}"]
    if view_model.context.model_endpoint:
        items.append(f"Model route: {view_model.context.model_endpoint}")
    if view_model.context.model_decision:
        items.append(f"Route decision: {view_model.context.model_decision}")
    if view_model.usage is not None:
        usage = view_model.usage
        items.append(
            f"Usage: {usage.prompt_tokens} input, "
            f"{usage.completion_tokens} output tokens across "
            f"{usage.call_count} calls ({usage.model_name})"
        )
    items.extend(
        f"{usage.usage_id}: {usage.call_count} calls, "
        f"{usage.prompt_tokens + usage.completion_tokens} tokens"
        for usage in view_model.usage_by_purpose
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
