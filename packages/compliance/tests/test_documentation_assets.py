# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Static tests for runnable documentation assets."""

from __future__ import annotations

import json
from pathlib import Path


def test_terra_jupyter_demo_notebook_covers_runtime_surfaces() -> None:
    notebook = json.loads(
        (_repo_root() / "docs" / "terra-jupyter-demo.ipynb").read_text(encoding="utf-8")
    )
    cells = notebook["cells"]
    sources = ["".join(cell["source"]) for cell in cells]
    combined = "\n".join(sources)

    assert notebook["nbformat"] == 4
    assert "Terra-Style Jupyter Demo" in sources[0]
    assert "NotebookSession" in combined
    assert "jupyter_proxy_url(port=8767)" in combined
    assert "offline_stack_smoke.sh" in combined
    assert "start_web_ui.sh" in combined
    assert "heartwood --workspace /home/jupyter/heartwood-workspace/sessions" in combined
    assert "session.detect()" in combined
    assert 'session.run("run the synthetic workflow")' in combined
    assert "session.audit_export()" in combined
    assert "build_widget_spec" in combined
    assert "synthetic" in combined
    assert "live Terra validation pass still needs to confirm" in combined
    for cell in cells:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
