# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native gateway lifetime leases coordinated by the project initialization lock."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.persistence import NativeLockUnavailableError, native_file_lock


class ProjectOwnershipError(RuntimeError):
    """Raised when project maintenance and a live gateway would overlap."""


class ProjectActivity:
    """Own one process-shared lease for the lifetime of a gateway's project access."""

    def __init__(self, project: ProjectContext) -> None:
        self.project = project
        self._identity = uuid4().hex
        self._lock = RLock()
        self._lease: FileLock | None = None
        self._closed = False

    def acquire_existing(self) -> None:
        """Register before accessing existing state without creating unconfirmed state."""
        with self._lock:
            self._require_open()
            if self._lease is not None or not self.project.state_exists():
                return
            with _admission(self.project, timeout=5):
                if not self.project.state_exists():
                    raise ProjectStateError("project state changed before gateway admission")
                self._register()

    def initialize(self) -> None:
        """Initialize and register atomically before releasing project admission."""
        with self._lock:
            self._require_open()
            if self._lease is not None:
                self.project.initialize()
                return
            # Validate before the native lock creates its parent for first initialization.
            self.project.state_exists()
            with _admission(self.project, timeout=5):
                self.project.initialize()
                self._register()

    def close(self) -> None:
        """Release ownership only after the gateway has drained all its work."""
        with self._lock:
            if self._lease is not None:
                self._lease.release()
                self._lease = None
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise ProjectOwnershipError("gateway project ownership is closed")

    def _register(self) -> None:
        directory = _registry(self.project)
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / f"{self._identity}.lock"
        if path.exists() or path.is_symlink():
            _validate_lease(path)
        lease = FileLock(path, timeout=0, mode=0o600, fallback_to_soft=False, thread_local=False)
        try:
            lease.acquire()
        except FileLockTimeout as error:
            raise ProjectOwnershipError("gateway project lease is already held") from error
        except OSError as error:
            raise NativeLockUnavailableError(
                "project storage does not support required gateway leases"
            ) from error
        self._lease = lease


@contextmanager
def project_gateway_maintenance(project: ProjectContext) -> Iterator[None]:
    """Exclude registered gateways and new registration using existing native locks."""
    if not project.state_exists():
        raise ProjectStateError("gateway maintenance requires initialized project state")
    with _admission(project, timeout=0), ExitStack() as leases:
        if not project.state_exists():
            raise ProjectStateError("project state changed before gateway maintenance")
        directory = _registry(project)
        if directory.exists():
            for path in sorted(directory.iterdir()):
                _validate_lease(path)
                try:
                    leases.enter_context(native_file_lock(path, timeout=0, reentrant=False))
                except NativeLockUnavailableError as error:
                    if isinstance(error.__cause__, FileLockTimeout):
                        raise ProjectOwnershipError(
                            "project maintenance requires all gateways to stop"
                        ) from error
                    raise
        yield


@contextmanager
def _admission(project: ProjectContext, *, timeout: float) -> Iterator[None]:
    with ExitStack() as locks:
        try:
            locks.enter_context(
                native_file_lock(project.state_lock_path, timeout=timeout, reentrant=False)
            )
        except NativeLockUnavailableError as error:
            if error.__cause__ is None or isinstance(error.__cause__, FileLockTimeout):
                raise ProjectOwnershipError(
                    "gateway admission is busy with initialization or maintenance"
                ) from error
            raise
        yield


def _registry(project: ProjectContext) -> Path:
    directory = project.runtime_dir / "gateway-leases"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ProjectStateError("gateway lease registry must be a regular directory")
    return directory


def _validate_lease(path: Path) -> None:
    try:
        valid_name = path.suffix == ".lock" and UUID(hex=path.stem).hex == path.stem
    except ValueError:
        valid_name = False
    if not valid_name or path.is_symlink() or not path.is_file() or path.stat().st_size:
        raise ProjectStateError("gateway lease records must be empty regular UUID-named lock files")
