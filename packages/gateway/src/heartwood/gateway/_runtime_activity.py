# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project exclusion and durable launch intent for the native inference supervisor."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from heartwood.gateway._project import ProjectContext, ProjectStateError
from heartwood.gateway._project_activity import ProjectActivity, ProjectOwnershipError
from heartwood.persistence import (
    native_file_lock,
    read_private_json,
    unlink_durable,
    write_private_json_atomic,
)


class ManagedRuntimeActivity:
    """Record the supervisor's spawn intent and confirmed child exit."""

    def __init__(self, project: ProjectContext) -> None:
        self._project = project
        self._active = False
        self._intent = {
            "schema_version": "heartwood.managed-runtime-intent.v1",
            "launch_id": uuid4().hex,
            "workspace": str(project.root),
            "launcher_pid": os.getpid(),
        }

    def before_spawn(self) -> None:
        """Persist intent before a subprocess may exist; never overwrite prior intent."""
        self._require_active()
        _require_resolved(self._project)
        write_private_json_atomic(self._project.managed_runtime_intent_path, self._intent)

    def confirm_exit(self) -> None:
        """Retire this intent after the supervisor has observed the child's exit."""
        self._require_active()
        path = self._project.managed_runtime_intent_path
        if read_private_json(path) != self._intent:
            raise ProjectOwnershipError(
                "managed inference launch intent changed during supervision"
            )
        unlink_durable(path)

    def _require_active(self) -> None:
        if not self._active:
            raise ProjectOwnershipError("managed inference supervision scope is closed")


@contextmanager
def managed_runtime_activity(project: ProjectContext) -> Iterator[ManagedRuntimeActivity]:
    """Hold project ownership and serialize native inference supervision."""
    activity = ProjectActivity(project)
    try:
        activity.acquire_existing()
        if not project.state_exists():
            raise ProjectStateError("native inference requires initialized project state")
        with native_file_lock(
            project.runtime_dir / ".managed-inference.lock", timeout=0, reentrant=False
        ):
            _require_resolved(project)
            runtime = ManagedRuntimeActivity(project)
            runtime._active = True
            try:
                yield runtime
            finally:
                runtime._active = False
    finally:
        activity.close()


def _require_resolved(project: ProjectContext) -> None:
    path = project.managed_runtime_intent_path
    if path.exists() or path.is_symlink():
        raise ProjectOwnershipError(
            "managed inference outcome is unresolved; reconcile runtime termination before launch"
        )
