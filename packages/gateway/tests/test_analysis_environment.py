# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Real offline reconstruction and confined setup failures without model calls."""

import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest
import tomli_w

from heartwood.gateway import ProjectContext
from heartwood.gateway._analysis_environment import (
    environment_directory,
    reconstruct_environment,
)
from heartwood.gateway._environment_probe import capture_environment, require_environment
from heartwood.gateway.python_environment import inspect_verification_environment


def _expected(project: Path) -> None:
    value = inspect_verification_environment().model_copy(
        update={"packages": (("synthetic-analysis", "1.0"),)}
    )
    (project / "expected.json").write_text(value.model_dump_json())


def test_offline_reconstruction_installs_only_locked_analysis_dependencies(
    tmp_path: Path,
    analysis_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = inspect_verification_environment()
    assert "synthetic-analysis" not in dict(before.packages)
    _expected(tmp_path)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", sys.prefix)
    monkeypatch.setenv("UV_PYTHON", "/must-not-be-used")
    monkeypatch.setenv("UV_NO_VERIFY_HASHES", "1")
    monkeypatch.setenv("UV_INDEX_URL", "https://secret@must-not-be-used.invalid")
    project = ProjectContext(tmp_path)
    python = reconstruct_environment(
        project,
        environment_id="a" * 64,
        lockfile=analysis_lock.name,
        expected="expected.json",
    )
    observed = inspect_verification_environment(python)
    assert dict(observed.packages) == {"synthetic-analysis": "1.0"}
    result = subprocess.run(
        [str(python), "-I", "-c", "from synthetic_analysis import mean; print(mean([1,3,5]))"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "3.0\n"
    capture_environment(project, "observation", python)
    require_environment(project, "observation/environment.json", python)
    assert inspect_verification_environment() == before
    from heartwood.gateway import SessionGateway
    from heartwood.notebook import NotebookSession

    gateway = SessionGateway(project=project, env={})
    try:
        relative_python = str(python.relative_to(tmp_path))
        assert gateway.verification_environment(python=relative_python) == observed
        notebook = NotebookSession(gateway=gateway, session_id="analysis")
        assert notebook.verification_environment(python=relative_python) == observed
        assert not gateway._services
    finally:
        gateway.stop()
    with pytest.raises(FileExistsError):
        reconstruct_environment(
            project,
            environment_id="a" * 64,
            lockfile=analysis_lock.name,
            expected="expected.json",
        )


def test_bad_wheel_hash_cannot_create_successful_environment_evidence(
    tmp_path: Path,
    analysis_lock: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    _expected(tmp_path)
    lock = tomllib.loads(analysis_lock.read_text())
    lock["packages"][0]["wheels"][0]["hashes"]["sha256"] = "0" * 64
    analysis_lock.write_text(tomli_w.dumps(lock))
    with pytest.raises(subprocess.CalledProcessError):
        reconstruct_environment(
            ProjectContext(tmp_path),
            environment_id="b" * 64,
            lockfile=analysis_lock.name,
            expected="expected.json",
        )
    assert not (tmp_path / "observation").exists()
    assert "Hash mismatch" in capfd.readouterr().err
    assert "synthetic-analysis" not in dict(inspect_verification_environment().packages)


def test_analysis_startup_does_not_inherit_provider_credentials(
    tmp_path: Path,
    analysis_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _expected(tmp_path)
    marker = tmp_path / "credential-leaked"
    wheel = next(tmp_path.glob("*.whl"))
    with ZipFile(wheel, "a") as archive:
        archive.writestr(
            "synthetic_startup.pth",
            "import os; from pathlib import Path; "
            f"Path({str(marker)!r}).touch() if os.environ.get('OPENAI_API_KEY') else None\n",
        )
    lock = tomllib.loads(analysis_lock.read_text())
    lock["packages"][0]["wheels"][0]["hashes"]["sha256"] = hashlib.sha256(
        wheel.read_bytes()
    ).hexdigest()
    analysis_lock.write_text(tomli_w.dumps(lock))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    python = reconstruct_environment(
        ProjectContext(tmp_path),
        environment_id="1" * 64,
        lockfile=analysis_lock.name,
        expected="expected.json",
    )
    assert dict(inspect_verification_environment(python).packages) == {"synthetic-analysis": "1.0"}
    assert not marker.exists()
    subprocess.run(
        [str(python), "-I", "-c", "pass"],
        env={"OPENAI_API_KEY": "synthetic-test-key"},
        check=True,
    )
    assert marker.exists(), "The synthetic startup hook must actually execute"


@pytest.mark.parametrize("source", ["directory", "vcs", "archive"])
def test_source_builds_are_rejected_before_execution(
    tmp_path: Path,
    analysis_lock: Path,
    source: str,
) -> None:
    _expected(tmp_path)
    lock = tomllib.loads(analysis_lock.read_text())
    lock["packages"][0][source] = {"path": "untrusted"}
    analysis_lock.write_text(tomli_w.dumps(lock))
    with pytest.raises(ValueError, match="source builds"):
        reconstruct_environment(
            ProjectContext(tmp_path),
            environment_id="c" * 64,
            lockfile=analysis_lock.name,
            expected="expected.json",
        )


@pytest.mark.parametrize("url", ["http://example.org/a.whl", "https://token@example.org/a.whl"])
def test_credentialed_and_insecure_wheel_urls_are_rejected(
    tmp_path: Path,
    analysis_lock: Path,
    url: str,
) -> None:
    _expected(tmp_path)
    lock = tomllib.loads(analysis_lock.read_text())
    wheel = lock["packages"][0]["wheels"][0]
    wheel.pop("path")
    wheel["url"] = url
    analysis_lock.write_text(tomli_w.dumps(lock))
    with pytest.raises(ValueError, match="HTTPS without credentials"):
        reconstruct_environment(
            ProjectContext(tmp_path),
            environment_id="d" * 64,
            lockfile=analysis_lock.name,
            expected="expected.json",
        )


def test_local_wheel_cannot_follow_a_symbolic_link(tmp_path: Path, analysis_lock: Path) -> None:
    _expected(tmp_path)
    wheel = next(tmp_path.glob("*.whl"))
    stored = wheel.with_suffix(".saved")
    wheel.rename(stored)
    wheel.symlink_to(stored)
    with pytest.raises(OSError, match="symbolic link"):
        reconstruct_environment(
            ProjectContext(tmp_path),
            environment_id="e" * 64,
            lockfile=analysis_lock.name,
            expected="expected.json",
        )


def test_changed_platform_and_escaping_identifiers_fail_before_setup(
    tmp_path: Path,
    analysis_lock: Path,
) -> None:
    _expected(tmp_path)
    path = tmp_path / "expected.json"
    value = json.loads(path.read_text())
    value["machine"] = "other-machine"
    path.write_text(json.dumps(value))
    project = ProjectContext(tmp_path)
    with pytest.raises(ValueError, match="platform differs"):
        reconstruct_environment(
            project,
            environment_id="f" * 64,
            lockfile=analysis_lock.name,
            expected=path.name,
        )
    assert not project.state_root.exists()
    with pytest.raises(ValueError, match="identifier"):
        environment_directory(project, "../escape")
