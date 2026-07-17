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
import sys
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
    [
        "0.1.0",
        "1.0.0",
        "0.2.0-beta.1",
        "2.3.4-rc.1",
        "2.3.4+build.7",
        "2.3.4-rc.1+build.7",
    ],
)
def test_release_versions_accept_strict_semver(version: str) -> None:
    assert _release_verifier().valid_semver(version)


@pytest.mark.parametrize(
    "version",
    ["v1.0.0", "01.0.0", "1.01.0", "1.0.01", "1.0", "1.0.0-01", "edge", ""],
)
def test_release_versions_reject_non_semver(version: str) -> None:
    assert not _release_verifier().valid_semver(version)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.2.0", False),
        ("0.2.0+build.7", False),
        ("0.2.0-alpha.1", True),
        ("0.2.0-beta.1", True),
        ("0.2.0-rc.1+build.7", True),
        ("0.2.0-preview.1", True),
    ],
)
def test_release_versions_classify_every_semver_prerelease(version: str, expected: bool) -> None:
    assert _release_verifier().is_prerelease(version) is expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.2.0", "0.2.0"),
        ("0.2.0-alpha.1", "0.2.0a1"),
        ("0.2.0-beta.1", "0.2.0b1"),
        ("0.2.0-preview.1", "0.2.0rc1"),
        ("0.2.0-rc.1", "0.2.0rc1"),
        ("0.2.0-dev.1", "0.2.0.dev1"),
        ("0.2.0-beta.1+BUILD-07", "0.2.0b1+build.7"),
    ],
)
def test_release_versions_map_to_canonical_python_metadata(version: str, expected: str) -> None:
    assert _release_verifier().python_package_version(version) == expected


def test_release_versions_reject_unrepresentable_python_prerelease() -> None:
    with pytest.raises(ValueError, match="Python packages support prerelease identifiers"):
        _release_verifier().python_package_version("0.2.0-canary.1")


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.2.0", "false\n"), ("0.2.0-beta.1", "true\n")],
)
def test_release_prerelease_output_is_workflow_safe(version: str, expected: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "deploy/verify_release_candidate.py",
            "--version",
            version,
            "--print-prerelease",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == expected
    assert result.stderr == ""


def test_release_source_versions_match_candidate() -> None:
    assert _release_verifier().source_version_errors(Path.cwd(), _declared_version()) == []


def test_release_source_versions_reject_other_candidate() -> None:
    errors = _release_verifier().source_version_errors(Path.cwd(), "999.999.999")
    assert errors
    assert f"packages/cli/pyproject.toml: {_declared_version()}" in errors


def test_current_release_guides_match_declared_version() -> None:
    assert _release_verifier().source_version_errors(Path.cwd(), _declared_version()) == []


def test_prerelease_sources_use_semver_and_python_lock_uses_pep440(
    tmp_path: Path,
) -> None:
    version = "0.2.0-beta.1"
    (tmp_path / "VERSION.toml").write_text(f'version = "{version}"\n', encoding="utf-8")
    python_package = tmp_path / "packages" / "cli"
    python_module = python_package / "src" / "heartwood" / "cli"
    python_module.mkdir(parents=True)
    (python_package / "pyproject.toml").write_text(
        f'[project]\nname = "heartwood-cli"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (python_module / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    web_package = tmp_path / "packages" / "webui"
    web_package.mkdir(parents=True)
    (web_package / "package.json").write_text(f'{{"version": "{version}"}}\n', encoding="utf-8")
    (web_package / "package-lock.json").write_text(
        f'{{"version": "{version}", "packages": {{"": {{"version": "{version}"}}}}}}\n',
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "verified" / "example"
    skill.mkdir(parents=True)
    skill_metadata = skill / "metadata.json"
    skill_metadata.write_text(
        f'{{"heartwood.version": "{version}"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "heartwood-cli"\nversion = "0.2.0b1"\n',
        encoding="utf-8",
    )
    documentation = {
        "container-images.md": f"heartwood:{version}",
        "carina-cli.md": (f"releases/download/{version}/heartwood-installer\n--version {version}"),
        "platform-support.md": f"Release `{version}`\n`{version}-terra`",
        "releases.md": f"-f version={version}",
        "terra-jupyter-demo.md": f"heartwood:{version}-terra",
    }
    docs = tmp_path / "docs"
    docs.mkdir()
    for name, content in documentation.items():
        (docs / name).write_text(content, encoding="utf-8")

    assert _release_verifier().source_version_errors(tmp_path, version) == []

    skill_metadata.write_text('{"heartwood.version": "0.2.0-beta.2"}\n', encoding="utf-8")
    assert (
        "skills/verified/example/metadata.json: 0.2.0-beta.2"
        in _release_verifier().source_version_errors(tmp_path, version)
    )

    skill_metadata.write_text("{}\n", encoding="utf-8")
    assert (
        "skills/verified/example/metadata.json: heartwood.version must be a string"
        in _release_verifier().source_version_errors(tmp_path, version)
    )

    skill_metadata.write_text('{"heartwood.version": 2}\n', encoding="utf-8")
    assert (
        "skills/verified/example/metadata.json: heartwood.version must be a string"
        in _release_verifier().source_version_errors(tmp_path, version)
    )

    skill_metadata.write_text("[]\n", encoding="utf-8")
    assert (
        "skills/verified/example/metadata.json: expected a JSON object"
        in _release_verifier().source_version_errors(tmp_path, version)
    )


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
    validation = Path(".github/workflows/validate.yml").read_text(encoding="utf-8")
    assert "--required-check 'Release Candidate Ready'" in workflow
    assert "python3 deploy/verify_model_sources.py --source-root ." in workflow
    assert (
        "python3 deploy/verify_model_sources.py --source-root . --allow-unavailable" in validation
    )
    assert "for attempt in" not in workflow
    assert "sleep 30" not in workflow
    assert "release-required-checks.txt" not in workflow


def test_documentation_is_validated_continuously_and_published_from_releases() -> None:
    documentation = Path(".github/workflows/documentation.yml").read_text(encoding="utf-8")
    publication = Path(".github/workflows/publish-documentation.yml").read_text(encoding="utf-8")
    publisher = Path("deploy/publish-versioned-documentation.sh").read_text(encoding="utf-8")
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    release = Path(".github/workflows/create-release.yml").read_text(encoding="utf-8")

    assert "  workflow_call:" in documentation
    assert "  workflow_dispatch:" in documentation
    assert "  push:" not in documentation
    assert "  pull_request:" not in documentation
    assert "  release:" not in documentation
    assert "zensical build --clean --strict" in documentation
    assert "uv run --no-sync bash deploy/tests/versioned_documentation_smoke.sh" in documentation
    assert "gh release view" not in documentation
    assert "actions/upload-pages-artifact" not in documentation
    assert "pages: write" not in documentation
    assert "id-token: write" not in documentation
    assert "  workflow_call:" in publication
    assert "  workflow_dispatch:" in publication
    assert "ref: refs/tags/${{ inputs.version }}" in publication
    assert "--json isDraft,isPrerelease,tagName,targetCommitish" in publication
    assert 'channel="preview"' in publication
    assert 'channel="stable"' in publication
    assert "DOCUMENTATION_BRANCH: gh-pages" in publication
    assert "${DOCUMENTATION_COMMIT}:refs/heads/${DOCUMENTATION_BRANCH}" in publication
    assert "deploy/publish-versioned-documentation.sh" in publication
    assert "${RUNNER_TEMP}/verify_release_candidate.py" in publication
    assert 'python3 "${RUNNER_TEMP}/verify_release_candidate.py"' in publication
    assert "published-site/${RELEASE_VERSION}/index.html" in publication
    assert "published-site/${DOCUMENTATION_CHANNEL}/index.html" in publication
    assert 'git rev-parse --verify "${DOCUMENTATION_BRANCH}^{commit}"' in publication
    assert "actions/upload-pages-artifact@v5" in publication
    assert "actions/deploy-pages@v5" in publication
    assert "Verify the deployed documentation" in publication
    assert '"${documentation_root}/${path}"' in publication
    assert '"${documentation_root}/versions.json"' in publication
    assert "name: github-pages" in publication
    assert "group: github-pages" in publication
    mike_requirement = (
        "mike @ git+https://github.com/squidfunk/mike.git@0f62791256ebeba60d20d2f1d8fe6ec3b7d1e2b3"
    )
    assert mike_requirement in pyproject
    assert mike_requirement in publication
    assert 'python3 "${release_verifier}" --version "${version}" --version-only' in publisher
    assert "git check-ref-format --branch" in publisher
    assert 'git remote get-url -- "${remote}"' in publisher
    assert "published documentation for ${version} differs" in publisher
    assert 'git push -- "${remote}" "refs/heads/${branch}:refs/heads/${branch}"' in publisher
    release_checkout = publication.index("- name: Checkout the exact release")
    release_verification = publication.index("- name: Verify the published release and channel")
    authentication = publication.index("- name: Enable version store authentication")
    artifact_validation = publication.index("- name: Stage the complete Pages artifact")
    push = publication.index("- name: Push the validated version store")
    assert "persist-credentials: false" in publication[release_checkout:release_verification]
    assert release_checkout < release_verification < artifact_validation < authentication < push
    assert "persist-credentials: true" in publication[authentication:push]
    assert "DOCUMENTATION_COMMIT: ${{ steps.version_store.outputs.commit }}" in publication
    assert "prerelease: ${{ steps.release.outputs.prerelease }}" in release
    assert "--print-prerelease" in release
    assert "release_flags=(--draft --latest=false)" in release
    assert "release_flags+=(--prerelease --latest=false)" in release
    assert "release_flags+=(--prerelease=false --latest)" in release
    assert "needs: [verify, publish]" in release
    assert "if: needs.verify.outputs.prerelease == 'false'" not in release
    documentation_job = release.split("  documentation:\n", maxsplit=1)[1]
    assert "contents: write" in documentation_job
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
