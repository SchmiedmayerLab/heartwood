# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from heartwood.cli._workspace_presentation import (
    format_workspace_changes,
    format_workspace_diff,
    format_workspace_file,
    format_workspace_tree,
)
from heartwood.schemas import (
    WorkspaceChangesResponse,
    WorkspaceDiffResponse,
    WorkspaceFileResponse,
    WorkspaceLimitsResponse,
    WorkspaceTreeResponse,
)


def _limits() -> WorkspaceLimitsResponse:
    return {
        "max_tree_entries": 2_000,
        "max_tree_depth": 8,
        "max_file_bytes": 512 * 1_024,
        "max_file_lines": 10_000,
        "max_change_entries": 500,
        "max_diff_bytes": 1_024 * 1_024,
    }


def test_workspace_tree_formatter_labels_empty_and_truncated_results() -> None:
    tree: WorkspaceTreeResponse = {
        "schema_version": "heartwood.workspace-tree.v1",
        "path": ".",
        "status": "truncated",
        "entries": [],
        "truncated": True,
        "limits": _limits(),
    }

    assert format_workspace_tree(tree) == (
        "Project files · .",
        "(no visible project files)",
        "[bounded project tree truncated]",
    )


def test_workspace_tree_formatter_marks_nested_unsupported_entries() -> None:
    tree: WorkspaceTreeResponse = {
        "schema_version": "heartwood.workspace-tree.v1",
        "path": "analysis",
        "status": "available",
        "entries": [
            {
                "path": "analysis/results",
                "name": "results",
                "kind": "directory",
                "depth": 1,
                "size_bytes": None,
            },
            {
                "path": "analysis/results/socket",
                "name": "socket",
                "kind": "unsupported",
                "depth": 2,
                "size_bytes": None,
            },
        ],
        "truncated": False,
        "limits": _limits(),
    }

    assert format_workspace_tree(tree) == (
        "Project files · analysis",
        "results/",
        "  socket [unavailable]",
    )


def test_workspace_file_and_change_formatters_preserve_explicit_states() -> None:
    file: WorkspaceFileResponse = {
        "schema_version": "heartwood.workspace-file.v1",
        "path": "analysis.py",
        "status": "truncated",
        "content": "answer = 42\n",
        "size_bytes": 200,
        "bytes_read": 12,
        "line_count": 1,
        "truncated": True,
        "message": "File content was truncated by Heartwood.",
    }
    changes: WorkspaceChangesResponse = {
        "schema_version": "heartwood.workspace-changes.v1",
        "status": "truncated",
        "source": "session-actions",
        "changes": [
            {
                "path": "analysis.py",
                "status": "added",
                "source": "session-action",
                "action_ids": ["call-1"],
            },
            {
                "path": "removed.py",
                "status": "deleted",
                "source": "session-action",
                "action_ids": ["call-2"],
            },
            {
                "path": "updated.py",
                "status": "modified",
                "source": "session-action",
                "action_ids": ["call-3"],
            },
        ],
        "truncated": True,
        "message": "Showing session-attributed file actions.",
        "limits": _limits(),
    }

    assert format_workspace_file(file) == (
        "File · analysis.py · truncated",
        "File content was truncated by Heartwood.",
        "",
        "answer = 42\n",
    )
    assert format_workspace_changes(changes) == (
        "Project changes · Session actions",
        "Showing session-attributed file actions.",
        "A  analysis.py",
        "D  removed.py",
        "M  updated.py",
        "[truncated after 500 entries]",
    )


def test_workspace_change_formatter_labels_an_unavailable_empty_result() -> None:
    changes: WorkspaceChangesResponse = {
        "schema_version": "heartwood.workspace-changes.v1",
        "status": "unavailable",
        "source": "unavailable",
        "changes": [],
        "truncated": False,
        "message": None,
        "limits": _limits(),
    }

    assert format_workspace_changes(changes) == (
        "Project changes · Unavailable",
        "(no changes available)",
    )


def test_workspace_diff_formatter_handles_empty_and_added_files() -> None:
    unavailable: WorkspaceDiffResponse = {
        "schema_version": "heartwood.workspace-diff.v1",
        "path": "missing.py",
        "status": "unavailable",
        "source": "unavailable",
        "original": None,
        "modified": None,
        "truncated": False,
        "message": "The path is unavailable.",
    }
    unchanged: WorkspaceDiffResponse = {
        **unavailable,
        "path": "unchanged.py",
        "status": "available",
        "source": "git",
        "original": "answer = 42\n",
        "modified": "answer = 42\n",
        "message": None,
    }
    added: WorkspaceDiffResponse = {
        **unchanged,
        "path": "added.py",
        "original": "",
        "modified": "answer = 42\n",
    }

    assert format_workspace_diff(unavailable) == (
        "Change · missing.py · unavailable",
        "The path is unavailable.",
    )
    assert format_workspace_diff(unchanged) == (
        "Change · unchanged.py · available",
        "",
        "(no textual differences)",
    )
    assert format_workspace_diff(added) == (
        "Change · added.py · available",
        "",
        "--- a/added.py",
        "+++ b/added.py",
        "@@ -0,0 +1 @@",
        "+answer = 42",
    )


def test_workspace_formatters_render_terminal_controls_visibly() -> None:
    file: WorkspaceFileResponse = {
        "schema_version": "heartwood.workspace-file.v1",
        "path": "report.txt",
        "status": "available",
        "content": "before\x1b]0;spoofed\x07after\u202e\udcff\n",
        "size_bytes": 24,
        "bytes_read": 24,
        "line_count": 1,
        "truncated": False,
        "message": None,
    }
    diff: WorkspaceDiffResponse = {
        "schema_version": "heartwood.workspace-diff.v1",
        "path": "report.txt",
        "status": "available",
        "source": "git",
        "original": "safe\n",
        "modified": "unsafe\x1b[2J\u202e\n",
        "truncated": False,
        "message": None,
    }

    rendered = "\n".join((*format_workspace_file(file), *format_workspace_diff(diff)))

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "\u202e" not in rendered
    assert "\udcff" not in rendered
    assert "\\x1b" in rendered
    assert "\\x07" in rendered
    assert "\\u202e" in rendered
    assert "\\udcff" in rendered
