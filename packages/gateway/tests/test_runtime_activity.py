# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native inference exclusion survives uncertainty about a spawned process."""

import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

from heartwood.gateway import (
    ManagedRuntimeActivity,
    NativeLockUnavailableError,
    ProjectContext,
    ProjectOwnershipError,
    ProjectStateError,
    managed_runtime_activity,
)
from heartwood.gateway._project_activity import project_gateway_maintenance


def test_runtime_owns_project_before_spawn_and_until_cleanup(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    with managed_runtime_activity(project) as activity:
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(project):
            pytest.fail("model verification must exclude migration before spawn")
        with pytest.raises(NativeLockUnavailableError), managed_runtime_activity(project):
            pytest.fail("a second native supervisor must not start")
        activity.before_spawn()
        record = json.loads(project.managed_runtime_intent_path.read_bytes())
        assert record["schema_version"] == "heartwood.managed-runtime-intent.v1"
        assert record["workspace"] == str(tmp_path)
        assert project.managed_runtime_intent_path.stat().st_mode & 0o777 == 0o600
        activity.confirm_exit()
        with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(project):
            pytest.fail("post-exit cleanup must retain project ownership")
    assert not project.managed_runtime_intent_path.exists()
    with pytest.raises(ProjectOwnershipError, match="closed"):
        activity.before_spawn()
    with pytest.raises(ProjectOwnershipError, match="closed"):
        activity.confirm_exit()
    with project_gateway_maintenance(project):
        assert project.state_exists()


def test_runtime_cannot_initialize_an_unconfirmed_project(tmp_path: Path) -> None:
    with pytest.raises(ProjectStateError), managed_runtime_activity(ProjectContext(tmp_path)):
        pytest.fail("runtime admission must require an initialized project")
    assert list(tmp_path.iterdir()) == []


def test_maintenance_prevents_runtime_admission(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    with (
        project_gateway_maintenance(project),
        pytest.raises(ProjectOwnershipError),
        managed_runtime_activity(project),
    ):
        pytest.fail("a native runtime must not enter maintenance")
    assert not project.managed_runtime_intent_path.exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_unconfirmed_spawn_retains_intent_after_supervisor_scope_exits(
    tmp_path: Path, interrupted: bool
) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    try:
        with managed_runtime_activity(project) as activity:
            activity.before_spawn()
            if interrupted:
                raise InterruptedError("synthetic interrupted launch")
    except InterruptedError:
        pass
    before = project.managed_runtime_intent_path.read_bytes()
    with (
        pytest.raises(ProjectOwnershipError, match="unresolved"),
        managed_runtime_activity(project),
    ):
        pytest.fail("uncertain launch must not be retried automatically")
    with (
        pytest.raises(ProjectOwnershipError, match="unresolved"),
        project_gateway_maintenance(project),
    ):
        pytest.fail("launcher exit does not prove inference termination")
    assert project.managed_runtime_intent_path.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "directory", "malformed"])
def test_unknown_intent_fails_closed_without_modification(tmp_path: Path, kind: str) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    path = project.managed_runtime_intent_path
    outside = tmp_path / "outside"
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("preserve unrecognized record")
    with pytest.raises(ProjectOwnershipError), managed_runtime_activity(project):
        pytest.fail("unknown intent must not be overwritten")
    with pytest.raises(ProjectOwnershipError), project_gateway_maintenance(project):
        pytest.fail("unknown intent must not be ignored")
    assert not outside.exists()
    if kind == "malformed":
        assert path.read_text() == "preserve unrecognized record"


def test_changed_intent_is_not_removed_by_previous_supervisor(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    with managed_runtime_activity(project) as activity:
        activity.before_spawn()
        with pytest.raises(ProjectOwnershipError):
            ManagedRuntimeActivity(project).confirm_exit()
        project.managed_runtime_intent_path.write_text('{"different": "record"}')
        with pytest.raises(ProjectOwnershipError):
            activity.confirm_exit()
    assert project.managed_runtime_intent_path.read_text() == '{"different": "record"}'


def test_crashed_launcher_leaves_live_child_and_blocks_maintenance(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    script = """
import os
import subprocess
import sys
from pathlib import Path
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
from heartwood.gateway import ProjectContext, managed_runtime_activity
with managed_runtime_activity(ProjectContext(Path(sys.argv[1]))) as activity:
    activity.before_spawn()
    subprocess.Popen([sys.executable, '-u', '-c', '''
import select
import sys
print('child-ready', flush=True)
if select.select([sys.stdin], [], [], 10)[0]:
    sys.stdin.readline()
print('child-stopped', flush=True)
'''], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        assert select.select([process.stdout], [], [], 15)[0]
        assert process.stdout.readline().strip() == "child-ready"
        assert process.wait(timeout=5) == 0
        with (
            pytest.raises(ProjectOwnershipError, match="unresolved"),
            project_gateway_maintenance(project),
        ):
            pytest.fail("a dead launcher must not imply its live child stopped")
    finally:
        output, errors = process.communicate("stop\n", timeout=15)
        assert process.returncode == 0, errors
        assert output.strip() == "child-stopped", errors
    assert project.managed_runtime_intent_path.is_file()
