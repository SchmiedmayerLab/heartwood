# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared plain-terminal presentation for bounded workspace responses."""

from __future__ import annotations

from difflib import unified_diff

from heartwood.gateway import display_safe_text as terminal_safe_text
from heartwood.schemas import (
    WorkspaceChangesResponse,
    WorkspaceDiffResponse,
    WorkspaceFileResponse,
    WorkspaceTreeResponse,
)


def format_workspace_tree(tree: WorkspaceTreeResponse) -> tuple[str, ...]:
    """Render a bounded project tree."""
    lines = [f"Project files · {terminal_safe_text(tree['path'])}"]
    for entry in tree["entries"]:
        indent = "  " * max(0, entry["depth"] - 1)
        marker = "/" if entry["kind"] == "directory" else ""
        unavailable = " [unavailable]" if entry["kind"] == "unsupported" else ""
        lines.append(f"{indent}{terminal_safe_text(entry['name'])}{marker}{unavailable}")
    if not tree["entries"]:
        lines.append("(no visible project files)")
    if tree["truncated"]:
        lines.append("[bounded project tree truncated]")
    return tuple(lines)


def format_workspace_file(file: WorkspaceFileResponse) -> tuple[str, ...]:
    """Render one bounded read-only file."""
    lines = [f"File · {terminal_safe_text(file['path'])} · {file['status']}"]
    if file["message"]:
        lines.append(terminal_safe_text(file["message"], preserve_newlines=True))
    if file["content"] is not None:
        lines.extend(("", terminal_safe_text(file["content"], preserve_newlines=True)))
    return tuple(lines)


def format_workspace_changes(changes: WorkspaceChangesResponse) -> tuple[str, ...]:
    """Render changed project paths."""
    source = {
        "git": "Git",
        "session-actions": "Session actions",
        "unavailable": "Unavailable",
    }[changes["source"]]
    lines = [f"Project changes · {source}"]
    if changes["message"]:
        lines.append(terminal_safe_text(changes["message"], preserve_newlines=True))
    for change in changes["changes"]:
        marker = {"added": "A", "deleted": "D", "modified": "M"}[change["status"]]
        lines.append(f"{marker}  {terminal_safe_text(change['path'])}")
    if not changes["changes"]:
        lines.append("(no changes available)")
    if changes["truncated"]:
        lines.append(f"[truncated after {changes['limits']['max_change_entries']:,} entries]")
    return tuple(lines)


def format_workspace_diff(diff: WorkspaceDiffResponse) -> tuple[str, ...]:
    """Render one bounded unified diff."""
    safe_path = terminal_safe_text(diff["path"])
    lines = [f"Change · {safe_path} · {diff['status']}"]
    if diff["message"]:
        lines.append(terminal_safe_text(diff["message"], preserve_newlines=True))
    if diff["original"] is None and diff["modified"] is None:
        return tuple(lines)
    original = (
        ""
        if diff["original"] is None
        else terminal_safe_text(diff["original"], preserve_newlines=True)
    )
    modified = (
        ""
        if diff["modified"] is None
        else terminal_safe_text(diff["modified"], preserve_newlines=True)
    )
    rendered = tuple(
        unified_diff(
            original.splitlines(),
            modified.splitlines(),
            fromfile=f"a/{safe_path}",
            tofile=f"b/{safe_path}",
            lineterm="",
        )
    )
    lines.extend(("", *(rendered or ("(no textual differences)",))))
    return tuple(lines)


__all__ = [
    "format_workspace_changes",
    "format_workspace_diff",
    "format_workspace_file",
    "format_workspace_tree",
]
