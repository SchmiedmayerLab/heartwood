# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import os
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def _release_verifier() -> ModuleType:
    path = Path("deploy/verify_release_candidate.py")
    spec = importlib.util.spec_from_file_location("verify_release_candidate", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_version() -> str:
    metadata = tomllib.loads(Path("VERSION.toml").read_text(encoding="utf-8"))
    version = metadata.get("version")
    assert isinstance(version, str)
    return version


@pytest.mark.parametrize(
    "version",
    ["0.1.0", "1.0.0", "2.3.4-rc.1", "2.3.4+build.7", "2.3.4-rc.1+build.7"],
)
def test_release_versions_accept_strict_semver(version: str) -> None:
    assert _release_verifier().valid_semver(version)


@pytest.mark.parametrize(
    "version",
    ["v1.0.0", "01.0.0", "1.01.0", "1.0.01", "1.0", "1.0.0-01", "edge", ""],
)
def test_release_versions_reject_non_semver(version: str) -> None:
    assert not _release_verifier().valid_semver(version)


def test_release_source_versions_match_candidate() -> None:
    assert _release_verifier().source_version_errors(Path.cwd(), _declared_version()) == []


def test_release_source_versions_reject_other_candidate() -> None:
    errors = _release_verifier().source_version_errors(Path.cwd(), "0.1.0")
    assert errors
    assert "packages/cli/pyproject.toml: 0.1.1" in errors


def test_current_release_guides_match_declared_version() -> None:
    assert _release_verifier().source_version_errors(Path.cwd(), _declared_version()) == []


def test_main_validation_owns_release_readiness_dependencies() -> None:
    workflow = Path(".github/workflows/main-validation.yml").read_text(encoding="utf-8")
    called_workflows = {
        "codeql": "codeql.yml",
        "containers": "container-image.yml",
        "container-smoke": "container-smoke.yml",
        "documentation": "documentation.yml",
        "secrets": "gitleaks.yml",
        "gpu-containers": "gpu-container-image.yml",
        "native-assets": "native-release.yml",
        "python": "python.yml",
        "validation": "validate.yml",
        "web-ui": "web-ui.yml",
    }
    for called_workflow in called_workflows.values():
        assert f"uses: ./.github/workflows/{called_workflow}" in workflow
        component = Path(f".github/workflows/{called_workflow}").read_text(encoding="utf-8")
        assert "  workflow_call:" in component
        assert "  push:" not in component
        assert "  pull_request:" not in component
        workflow_header = component.split("jobs:", maxsplit=1)[0]
        if "concurrency:" in workflow_header:
            assert "${{ github.workflow }}-${{ github.ref }}" in workflow_header
    readiness = workflow.split("  release-ready:\n", maxsplit=1)[1]
    assert "name: Release Candidate Ready" in readiness
    for job_id in called_workflows:
        assert f"      - {job_id}" in readiness
    assert 'if $event == "pull_request" then' in readiness
    assert '.containers.result == "skipped"' in readiness
    assert 'to_entries | all(.value.result == "success")' in readiness


def test_release_gate_is_fail_fast_and_uses_readiness_check() -> None:
    workflow = Path(".github/workflows/create-release.yml").read_text(encoding="utf-8")
    assert "--required-check 'Release Candidate Ready'" in workflow
    assert "for attempt in" not in workflow
    assert "sleep 30" not in workflow
    assert "release-required-checks.txt" not in workflow


def test_documentation_is_validated_continuously_and_published_from_releases() -> None:
    documentation = Path(".github/workflows/documentation.yml").read_text(encoding="utf-8")
    publication = Path(".github/workflows/publish-documentation.yml").read_text(
        encoding="utf-8"
    )
    release = Path(".github/workflows/create-release.yml").read_text(encoding="utf-8")

    assert "  workflow_call:" in documentation
    assert "  workflow_dispatch:" in documentation
    assert "  push:" not in documentation
    assert "  pull_request:" not in documentation
    assert "  release:" not in documentation
    assert "refs/tags/{0}" in documentation
    assert "gh release view" in documentation
    assert "--version-only" in documentation
    assert "zensical build --clean --strict" in documentation
    assert "actions/upload-pages-artifact@v5" in documentation
    assert "pages: write" not in documentation
    assert "id-token: write" not in documentation
    assert "  workflow_call:" in publication
    assert "  workflow_dispatch:" in publication
    assert "uses: ./.github/workflows/documentation.yml" in publication
    assert "publish_artifact: true" in publication
    assert "actions/deploy-pages@v5" in publication
    assert "Verify the deployed documentation" in publication
    assert '"${DOCUMENTATION_URL}"' in publication
    assert "name: github-pages" in publication
    assert "group: github-pages" in publication
    assert "needs: publish" in release
    assert "uses: ./.github/workflows/publish-documentation.yml" in release


def test_release_checks_require_latest_successful_run() -> None:
    verifier = _release_verifier()
    payload = {
        "check_runs": [
            {"id": 1, "name": "Python", "status": "completed", "conclusion": "success"},
            {"id": 2, "name": "Python", "status": "completed", "conclusion": "failure"},
            {"id": 3, "name": "Containers", "status": "in_progress", "conclusion": None},
        ]
    }
    incomplete, failed = verifier.check_status(payload, ["Python", "Containers", "Native"])
    assert incomplete == ["Containers", "Native"]
    assert failed == ["Python: failure"]


def test_release_checks_accept_paginated_successes() -> None:
    verifier = _release_verifier()
    payload = [
        {
            "check_runs": [
                {"id": 1, "name": "Python", "status": "completed", "conclusion": "success"}
            ]
        },
        {
            "check_runs": [
                {"id": 2, "name": "Containers", "status": "completed", "conclusion": "success"}
            ]
        },
    ]
    assert verifier.check_status(payload, ["Python", "Containers"]) == ([], [])


def test_release_checks_accept_successful_commit_status() -> None:
    verifier = _release_verifier()
    checks: dict[str, object] = {"check_runs": []}
    statuses = {"statuses": [{"context": "CodeQL", "state": "success"}]}
    assert verifier.check_status(checks, ["CodeQL"], statuses) == ([], [])


def test_release_checks_reject_failed_commit_status() -> None:
    verifier = _release_verifier()
    checks: dict[str, object] = {"check_runs": []}
    statuses = {"statuses": [{"context": "CodeQL", "state": "failure"}]}
    assert verifier.check_status(checks, ["CodeQL"], statuses) == ([], ["CodeQL: failure"])


def test_release_image_promotion_is_complete_and_idempotent(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    state = tmp_path / "published"
    log = tmp_path / "commands"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG}"
if [[ "$*" == *" inspect --raw "* ]]; then
  ref="${@: -1}"
  if [[ "${ref}" == *terra* ]]; then
    printf '%s\\n' '{"mediaType":"application/vnd.docker.distribution.manifest.v2+json","config":{"mediaType":"application/vnd.docker.container.image.v1+json"}}'
  elif [[ "${ref}" == *gpu-nvidia* ]]; then
    printf '%s\\n' '{"manifests":[{"platform":{"os":"linux","architecture":"amd64"}}]}'
  else
    printf '%s\\n' '{"manifests":[{"platform":{"os":"linux","architecture":"amd64"}},{"platform":{"os":"linux","architecture":"arm64"}}]}'
  fi
  exit 0
fi
if [[ "$*" == *" imagetools create "* ]]; then
  previous=""
  for argument in "$@"; do
    if [[ "${previous}" == "--tag" ]]; then
      printf '%s\\n' "${argument}" >> "${FAKE_DOCKER_STATE}"
      break
    fi
    previous="${argument}"
  done
  exit 0
fi
ref="${@: -1}"
if [[ "${ref}" != *":sha-"* ]] && ! grep --fixed-strings --line-regexp "${ref}" "${FAKE_DOCKER_STATE}" >/dev/null 2>&1; then
  exit 1
fi
printf '%s\\n' 'Name: fake' 'Digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    state.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_STATE": str(state),
    }
    command = [
        "deploy/promote-release-images.sh",
        "promote",
        "1.2.3+build.4",
        "a" * 40,
        "registry.example/heartwood",
    ]
    subprocess.run(command, check=True, env=env)
    subprocess.run(command, check=True, env=env)

    published = state.read_text(encoding="utf-8").splitlines()
    assert published == [
        "registry.example/heartwood:1.2.3_build.4",
        "registry.example/heartwood:1.2.3_build.4-terra",
        "registry.example/heartwood:1.2.3_build.4-gpu-nvidia",
        "registry.example/heartwood:1.2.3_build.4-terra-gpu-nvidia",
    ]
    commands = log.read_text(encoding="utf-8")
    assert commands.count("imagetools create") == 4
    assert commands.count("--prefer-index=false") == 2
