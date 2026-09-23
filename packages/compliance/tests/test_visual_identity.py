# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests that every surface uses the one generated Heartwood identity."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

from heartwood.cli._brand import DARK, LIGHT, MARK_CORE


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read(relative_path: str) -> str:
    return (_repo_root() / relative_path).read_text(encoding="utf-8")


def _generator() -> ModuleType:
    path = _repo_root() / "packages" / "cli" / "scripts" / "generate_brand_assets.py"
    spec = importlib.util.spec_from_file_location("generate_brand_assets", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_brand_assets_match_their_source() -> None:
    assert _generator().main(["--check"]) == 0


def test_documentation_readme_and_browser_use_the_generated_mark() -> None:
    theme = tomllib.loads(_read("zensical.toml"))["project"]["theme"]
    readme = _read("README.md")
    index = _read("packages/webui/index.html")
    rail = _read("packages/webui/src/components/SessionRail.tsx")

    assert theme["logo"] == "assets/brand/heartwood-mark.svg"
    assert theme["favicon"] == "assets/brand/heartwood-favicon.svg"
    assert "logo" not in theme["icon"]
    assert 'srcset="documentation/assets/brand/heartwood-hero-dark.svg"' in readme
    assert '<img alt="Heartwood" src="documentation/assets/brand/heartwood-hero-light.svg">' in (
        readme
    )
    assert 'href="./heartwood-favicon.svg"' in index
    assert "<HeartwoodMark" in rail
    assert "Sprout" not in rail


def test_shared_color_tokens_govern_the_browser_and_documentation() -> None:
    browser = _read("packages/webui/src/styles.css")
    documentation = _read("documentation/stylesheets/extra.css")

    for value in (
        LIGHT.primary,
        DARK.primary,
        LIGHT.surface,
        DARK.surface,
        LIGHT.foreground,
        DARK.foreground,
        LIGHT.muted_foreground,
        DARK.muted_foreground,
        MARK_CORE,
    ):
        assert value in browser, value
    for value in (LIGHT.primary, DARK.primary):
        assert value in documentation, value
