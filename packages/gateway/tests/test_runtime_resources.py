# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Installed resources cannot fall back to project-controlled files."""

from pathlib import Path

import pytest

from heartwood.gateway import _gateway


@pytest.mark.parametrize("editable", [False, True])
def test_gateway_prefers_its_packaged_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, editable: bool
) -> None:
    module = tmp_path / (
        "packages/gateway/src/heartwood/gateway/_gateway.py"
        if editable
        else "site-packages/heartwood/gateway/_gateway.py"
    )
    packaged = module.parent / "_bundled"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(_gateway, "__file__", str(module))
    assert _gateway._runtime_resource_root() == packaged


def test_gateway_missing_resources_reject_project_and_ancestor_decoys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "site-packages/heartwood/gateway/_gateway.py"
    module.parent.mkdir(parents=True)
    project = tmp_path / "research"
    project.mkdir()
    for root in (project, tmp_path):
        (root / "images/generic/local-runtime").mkdir(parents=True)
        (root / "vendor/heartwood-skills/skills").mkdir(parents=True)
        (root / "_bundled").mkdir()
    monkeypatch.setattr(_gateway, "__file__", str(module))
    monkeypatch.chdir(project)
    with pytest.raises(RuntimeError, match="missing bundled runtime resources"):
        _gateway._runtime_resource_root()


def test_gateway_editable_resources_require_the_exact_checkout_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "packages/gateway/src/heartwood/gateway/_gateway.py"
    module.parent.mkdir(parents=True)
    monkeypatch.setattr(_gateway, "__file__", str(module))
    assert _gateway._runtime_resource_root() == tmp_path


def test_gateway_shallow_install_reports_missing_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_gateway, "__file__", "/_gateway.py")
    with pytest.raises(RuntimeError, match="missing bundled runtime resources"):
        _gateway._runtime_resource_root()
