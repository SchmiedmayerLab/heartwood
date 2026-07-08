# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for synthetic fixture linting."""

from __future__ import annotations

from pathlib import Path

from heartwood.fixtures import lint_fixture_tree, main


def test_repository_fixtures_are_synthetic() -> None:
    findings = lint_fixture_tree(Path("fixtures"))
    assert findings == ()


def test_linter_rejects_direct_identifier(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.json"
    fixture.write_text('{"contact": "person@example.com"}\n', encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "direct-identifier.email"


def test_linter_rejects_parenthesized_phone_number(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.txt"
    fixture.write_text("contact=(415) 555-1212\n", encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "direct-identifier.phone"


def test_linter_rejects_secret_shape(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.env"
    fake_token = "ghp_" + ("1" * 36)
    fixture.write_text(f"TOKEN={fake_token}\n", encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert findings[0].rule == "secret.github-token"

    fixture = tmp_path / "bad.txt"
    fixture.write_text(f"token={fake_token}\n", encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert any(finding.rule == "secret.github-token" for finding in findings)


def test_linter_checks_toml_files(tmp_path: Path) -> None:
    fixture = tmp_path / "bundle.toml"
    fixture.write_text('source = "production"\n', encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert findings[0].rule == "live-source-marker"


def test_linter_rejects_provider_key_with_separators(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.txt"
    fake_key = "sk-" + "proj-" + ("a" * 32)
    fixture.write_text(f"api_key={fake_key}\n", encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert findings[0].rule == "secret.openai-key"


def test_linter_rejects_live_source_marker(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.json"
    fixture.write_text('{"source": "production"}\n', encoding="utf-8")
    findings = lint_fixture_tree(tmp_path)
    assert findings[0].rule == "live-source-marker"


def test_fixture_lint_cli_returns_nonzero_for_findings(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.csv"
    fixture.write_text("medical_record_number\n123\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
