<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Notebook

Notebook-facing Python API and minimal widget bridge for Heartwood sessions.

The package presents the gateway-owned session projection used by the CLI and browser as a typed notebook view model.
The projection includes the conversation, lifecycle, one atomic approval group, task plan, usage, specialist-agent lineage, live output, and activity without a notebook-specific event reducer.
The optional widget bridge renders that state with `ipywidgets` when available and falls back to deterministic widget specifications when it is not.
`NotebookSession` provides conversation, grouped action review, pause and resume, replay, and audit operations without a separate backend or project state.
