# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Gateway lifetime ownership excludes maintenance without serializing sessions."""

import errno
import select
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from filelock import FileLock

from heartwood.gateway import (
    ProjectContext,
    ProjectOwnershipError,
    ProjectStateError,
    RestGateway,
    RestRequest,
    SessionGateway,
)
from heartwood.gateway import _project_activity as activity_module
from heartwood.gateway._project_activity import project_gateway_maintenance
from heartwood.gateway._session_migration import staged_session_records
from heartwood.persistence import NativeLockUnavailableError
from heartwood.session import CommandKind, SessionCommand


def _projects(tmp_path: Path) -> tuple[ProjectContext, ProjectContext]:
    workspace = tmp_path / "research"
    workspace.mkdir()
    parent = tmp_path / "protected"
    parent.mkdir(mode=0o700)
    identity = uuid4()
    return ProjectContext(workspace), ProjectContext(
        workspace, protected_state=parent / str(identity), project_id=identity
    )


def test_idle_gateway_prevents_staging_until_successful_shutdown(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    try:
        gateway.initialize_project()
        with pytest.raises(RuntimeError, match="gateway"), staged_session_records(source, target):
            pytest.fail("an idle live gateway must prevent migration staging")
    finally:
        gateway.stop()
    with staged_session_records(source, target) as stage:
        assert (stage / "receipt.json").is_file()


def test_gateway_config_initialization_registers_ownership(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    try:
        gateway.config_store.save(gateway.config_store.load())
        with pytest.raises(RuntimeError, match="gateway"), staged_session_records(source, target):
            pytest.fail("configuration initialization must register the gateway")
    finally:
        gateway.stop()


def test_gateway_start_cannot_enter_initialized_project_during_staging(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    source.initialize()
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    try:
        with (
            staged_session_records(source, target),
            pytest.raises(RuntimeError, match="maintenance"),
        ):
            gateway.start()
    finally:
        gateway.stop()


def test_constructing_and_starting_an_unconfirmed_gateway_creates_no_files(tmp_path: Path) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={}, backend_id="deterministic")
    try:
        gateway.start()
        gateway.model_settings()
        assert list(tmp_path.iterdir()) == []
    finally:
        gateway.stop()
    assert list(tmp_path.iterdir()) == []


def test_read_only_api_admission_registers_existing_project_and_reports_maintenance(
    tmp_path: Path,
) -> None:
    source, _target = _projects(tmp_path)
    source.initialize()
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    try:
        with project_gateway_maintenance(source):
            response = RestGateway(gateway).handle(
                RestRequest(method="GET", path="/project/capabilities")
            )
            assert response.status_code == 409
            assert "maintenance" in str(response.body)
        gateway.platform_capabilities()
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(source):
            pytest.fail("an admitted read-only gateway can later write and must retain a lease")
    finally:
        gateway.stop()


def test_failed_shutdown_retains_gateway_lease_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    gateway.default_session()
    close = gateway.local_model_manager.close

    def incomplete() -> None:
        raise TimeoutError("synthetic model worker still running")

    monkeypatch.setattr(gateway.local_model_manager, "close", incomplete)
    try:
        with pytest.raises(TimeoutError, match="still running"):
            gateway.stop()
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(source):
            pytest.fail("failed shutdown must retain ownership")
    finally:
        monkeypatch.setattr(gateway.local_model_manager, "close", close)
        gateway.stop()
    with project_gateway_maintenance(source):
        assert source.state_exists()


def test_closed_gateway_configuration_store_cannot_reinitialize_ownership(tmp_path: Path) -> None:
    source, _target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    gateway.stop()
    with pytest.raises(ProjectOwnershipError, match="closed"):
        gateway.config_store.save(gateway.config_store.load())
    assert not source.state_root.exists()


def test_first_initialization_keeps_admission_until_gateway_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    initialized, release = Event(), Event()
    initialize = ProjectContext.initialize

    def pause_after_initialization(project: ProjectContext) -> None:
        initialize(project)
        initialized.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(ProjectContext, "initialize", pause_after_initialization)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            operation = executor.submit(gateway.default_session)
            try:
                assert initialized.wait(timeout=5)
                assert source.state_exists()
                with pytest.raises(ProjectOwnershipError), staged_session_records(source, target):
                    pytest.fail("initialization must not expose a gap before gateway registration")
            finally:
                release.set()
            operation.result(timeout=5)
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(source):
            pytest.fail("the registered gateway must retain ownership")
    finally:
        gateway.stop()


@contextmanager
def _child_gateway(source: ProjectContext) -> Iterator[subprocess.Popen[str]]:
    script = """
import os
import sys
from pathlib import Path
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.session import CommandKind, SessionCommand
gateway = SessionGateway(
    project=ProjectContext(Path(sys.argv[1])), env={}, backend_id='deterministic')
gateway.handle(SessionCommand(command_id='child-pause', session_id='session-child',
    kind=CommandKind.PAUSE, created_at='2026-01-01T00:00:00Z'))
print('ready', flush=True)
if sys.stdin.readline().strip() == 'exit-without-close':
    os._exit(0)
gateway.stop()
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(source.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        assert select.select([process.stdout], [], [], 15)[0], "child did not become ready"
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        try:
            output, errors = process.communicate(
                "stop\n" if process.poll() is None else None, timeout=15
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise
        assert process.returncode == 0, (output, errors)


def test_independent_gateway_processes_keep_concurrent_sessions_and_exclude_maintenance(
    tmp_path: Path,
) -> None:
    source, target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    try:
        gateway.handle(
            SessionCommand(
                command_id="parent-pause",
                session_id="session-parent",
                kind=CommandKind.PAUSE,
                created_at="2026-01-01T00:00:00Z",
            )
        )
        with _child_gateway(source):
            assert gateway._services["session-parent"].store.owns_writer
            assert (source.sessions_dir / "session-child" / "events.jsonl").is_file()
            with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(source):
                pytest.fail("live gateway processes must exclude maintenance")
            gateway.stop()
            with pytest.raises(ProjectOwnershipError), staged_session_records(source, target):
                pytest.fail("the child gateway still owns the project")
    finally:
        gateway.stop()
    with staged_session_records(source, target) as stage:
        assert (stage / "sessions" / "session-parent" / "events.jsonl").is_file()
        assert (stage / "sessions" / "session-child" / "events.jsonl").is_file()


def test_process_exit_releases_gateway_lease_without_trusting_stale_files(tmp_path: Path) -> None:
    source, target = _projects(tmp_path)
    with _child_gateway(source) as process:
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(source):
            pytest.fail("the live child gateway must exclude maintenance")
        assert process.stdin is not None
        process.stdin.write("exit-without-close\n")
        process.stdin.flush()
        process.wait(timeout=15)
    files = tuple((source.runtime_dir / "gateway-leases").iterdir())
    assert len(files) == 1
    with staged_session_records(source, target):
        assert files[0].is_file()
        assert files[0].stat().st_size == 0


def test_failed_maintenance_releases_previously_acquired_stale_leases(tmp_path: Path) -> None:
    source, _target = _projects(tmp_path)
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")
    try:
        gateway.initialize_project()
        stale = source.runtime_dir / "gateway-leases" / f"{'0' * 32}.lock"
        stale.touch(mode=0o600)
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(source):
            pytest.fail("the live gateway must prevent maintenance after the stale lease")
        with FileLock(stale, timeout=0, fallback_to_soft=False):
            assert stale.stat().st_size == 0
    finally:
        gateway.stop()
    with project_gateway_maintenance(source):
        assert source.state_exists()


def test_gateway_start_is_excluded_by_maintenance_in_another_process(tmp_path: Path) -> None:
    source, _target = _projects(tmp_path)
    source.initialize()
    script = """
import os
import sys
from pathlib import Path
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
from heartwood.gateway import ProjectContext, ProjectOwnershipError, SessionGateway
gateway = SessionGateway(
    project=ProjectContext(Path(sys.argv[1])), env={}, backend_id='deterministic')
try:
    gateway.start()
except ProjectOwnershipError:
    print('excluded')
else:
    raise AssertionError('gateway entered during maintenance')
finally:
    gateway.stop()
"""
    with project_gateway_maintenance(source):
        result = subprocess.run(
            [sys.executable, "-c", script, str(source.root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert result.stdout.strip() == "excluded"


@pytest.mark.parametrize("kind", ["registry-symlink", "record-symlink", "unknown", "nonempty"])
def test_gateway_maintenance_rejects_unsafe_lease_records(tmp_path: Path, kind: str) -> None:
    source, _target = _projects(tmp_path)
    source.initialize()
    directory = source.runtime_dir / "gateway-leases"
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "registry-symlink":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        path = directory / ("unknown" if kind == "unknown" else f"{uuid4().hex}.lock")
        if kind == "record-symlink":
            path.symlink_to(outside / "untouched")
        else:
            path.write_text("retain this data" if kind == "nonempty" else "")
    with pytest.raises(ProjectStateError), project_gateway_maintenance(source):
        pytest.fail("unsafe lease records must not be opened or ignored")
    assert list(outside.iterdir()) == []
    if kind == "nonempty":
        assert next(directory.iterdir()).read_text() == "retain this data"


def test_unsupported_gateway_leases_fail_before_api_work_and_allow_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _target = _projects(tmp_path)
    source.initialize()
    gateway = SessionGateway(project=source, env={}, backend_id="deterministic")

    def unavailable() -> None:
        raise OSError(errno.ENOTSUP, "synthetic unsupported lease")

    def unsupported_lock(
        path: Path, *, timeout: int, mode: int, fallback_to_soft: bool, thread_local: bool
    ) -> FileLock:
        lock = FileLock(
            path,
            timeout=timeout,
            mode=mode,
            fallback_to_soft=fallback_to_soft,
            thread_local=thread_local,
        )
        monkeypatch.setattr(lock, "acquire", unavailable)
        return lock

    try:
        with monkeypatch.context() as patch:
            patch.setattr(activity_module, "FileLock", unsupported_lock)
            with pytest.raises(NativeLockUnavailableError, match="gateway leases"):
                gateway.create_session()
            assert list(source.sessions_dir.iterdir()) == []
        assert gateway.default_session()["session_id"] == "session-main"
    finally:
        gateway.stop()
