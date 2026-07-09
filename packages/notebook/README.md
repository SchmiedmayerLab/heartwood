<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-notebook

Notebook-facing Python API and minimal widget bridge for Heartwood sessions.

The package presents the same session events consumed by the CLI as typed notebook view models. The optional widget bridge renders those models with `ipywidgets` when available and falls back to deterministic widget specifications when it is not.
