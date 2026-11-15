# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""The secret scan covers validated history and, on its own schedule, every branch and tag."""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MARKER = "synthetic-unreviewed-credential"
# Stands in for gitleaks: report a finding when the scanned history contains the marker.
_SCANNER = f"""#!/usr/bin/env bash
printf '%s\\n' "$@" >"${{SCAN_ARGUMENTS}}"
log_opts="${{3#--log-opts=}}"
if git log -p ${{log_opts}} | grep --quiet '{_MARKER}'; then
  echo 'finding: REDACTED'
  exit 1
fi
"""


def _workflow() -> dict[str, Any]:
    yaml = importlib.import_module("yaml")
    workflow: dict[str, Any] = yaml.load(
        (_ROOT / ".github/workflows/gitleaks.yml").read_text(), Loader=yaml.BaseLoader
    )
    return workflow


def _steps() -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in _workflow()["jobs"]["gitleaks"]["steps"]}


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Heartwood Test",
            "-c",
            "user.email=heartwood@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *arguments,
        ],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(directory: Path, content: str) -> None:
    (directory / "file.txt").write_text(content)
    _git(directory, "add", "file.txt")
    _git(directory, "commit", "-qm", "Change synthetic file")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A complete checkout of an origin whose secret is only on a branch without a pull request."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    _commit(origin, "reviewed\n")
    _git(origin, "checkout", "-q", "-b", "unreviewed")
    _commit(origin, f"token = {_MARKER}\n")
    _git(origin, "checkout", "-q", "main")
    _commit(origin, "tagged\n")
    _git(origin, "tag", "synthetic-tag")
    _git(origin, "reset", "-q", "--hard", "HEAD~1")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    (checkout / "gitleaks").write_text(_SCANNER)
    (checkout / "gitleaks").chmod(0o755)
    return checkout


def _scan(checkout: Path, all_refs: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-e", "-c", _steps()["Scan the validated history or every branch and tag"]["run"]],
        cwd=checkout,
        env={
            "ALL_REFS": str(all_refs).lower(),
            "PATH": os.environ["PATH"],
            "SCAN_ARGUMENTS": str(checkout.parent / "arguments"),
        },
        capture_output=True,
        text=True,
    )


def test_scheduled_scan_finds_a_secret_on_a_branch_without_a_pull_request(
    checkout: Path,
) -> None:
    validated = _scan(checkout, all_refs=False)
    assert validated.returncode == 0
    assert (checkout.parent / "arguments").read_text().splitlines()[:3] == [
        "git",
        ".",
        "--log-opts=HEAD",
    ]

    scheduled = _scan(checkout, all_refs=True)
    assert scheduled.returncode == 1
    assert _MARKER not in scheduled.stdout + scheduled.stderr
    assert (checkout.parent / "arguments").read_text().splitlines() == [
        "git",
        ".",
        "--log-opts=--all",
        "--no-banner",
        "--redact",
        "--exit-code",
        "1",
    ]


def test_scheduled_scan_refuses_a_checkout_without_every_branch_and_tag(
    checkout: Path,
) -> None:
    _git(checkout, "update-ref", "-d", "refs/remotes/origin/unreviewed")
    assert _scan(checkout, all_refs=True).returncode == 1
    assert not (checkout.parent / "arguments").exists()

    _git(checkout, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    _git(checkout, "tag", "-d", "synthetic-tag")
    refused = _scan(checkout, all_refs=True)
    assert refused.returncode == 1
    assert "does not hold refs/tags/synthetic-tag" in refused.stderr
    assert not (checkout.parent / "arguments").exists()


def test_scan_triggers_scope_and_pinned_release() -> None:
    workflow = _workflow()
    steps = _steps()

    assert workflow["name"] == "Secret Scan"
    assert set(workflow["on"]) == {"workflow_call", "workflow_dispatch", "schedule"}
    assert [entry["cron"] for entry in workflow["on"]["schedule"]] == ["17 10 * * *"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"]["gitleaks"]
    checkout = steps["Checkout repository"]["with"]
    assert checkout["fetch-depth"] == "0"
    assert checkout["persist-credentials"] == "false"
    # Calling workflows supply their own name, so only this workflow's runs scan every ref.
    scan = steps["Scan the validated history or every branch and tag"]
    assert scan["env"] == {"ALL_REFS": "${{ github.workflow == 'Secret Scan' }}"}
    assert steps["Install gitleaks"]["env"] == {
        "GITLEAKS_VERSION": "8.30.1",
        "GITLEAKS_SHA256": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    }
    assert "sha256sum --check -" in steps["Install gitleaks"]["run"]
    for entrypoint in ("main-validation.yml", "pull-request-validation.yml"):
        caller = (_ROOT / ".github/workflows" / entrypoint).read_text()
        assert "name: Secret Scan" not in caller.split("\njobs:", maxsplit=1)[0]
        assert "uses: ./.github/workflows/gitleaks.yml" in caller
