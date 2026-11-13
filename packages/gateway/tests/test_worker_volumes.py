# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Native staging rejects ambiguous ownership and preserves resources on uncertainty."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from heartwood.core_adapter.worker_attempts import WorkerAttemptError, WorkerAttemptIntent
from heartwood.gateway.worker_volumes import WorkerVolumes, WorkerVolumeSettings

_IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def volumes() -> WorkerVolumes:
    intent = WorkerAttemptIntent(
        project_id=uuid4(),
        session_id="synthetic",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest=_IMAGE,
        gateway_policy_sha256="b" * 64,
        sandbox_policy_sha256="c" * 64,
        input_manifest_sha256="d" * 64,
    )
    settings = WorkerVolumeSettings(
        docker_cli=Path("/opt/trusted/docker"),
        docker_host="unix:///var/run/docker.sock",
        workspace="synthetic",
        image_digest=_IMAGE,
    )
    return WorkerVolumes(settings, intent)


def _container(volumes: WorkerVolumes, *, role: str = "worker") -> dict[str, Any]:
    return {
        "Id": "native-id",
        "Name": "/"
        + (
            volumes.staging_name
            if role == "staging"
            else f"openshell-synthetic--{volumes.worker_name}-native-id"
        ),
        "Config": {"Labels": {**volumes.labels, "heartwood.role": role}},
        "Image": _IMAGE,
        "HostConfig": {"NetworkMode": "none"},
        "State": {"Running": False, "ExitCode": 0},
        "Mounts": [
            {"Destination": f"/{mount}", "Name": name, "Type": "volume", "RW": False}
            for mount, name in volumes.names.items()
        ],
    }


@pytest.mark.parametrize(
    "host",
    [
        "tcp://host:2375",
        "unix://host/socket",
        "unix:relative",
        "unix:///a/../socket",
        "unix:///socket?context=other",
        "unix:///socket#other",
        "unix:///%2e/socket",
    ],
)
def test_only_explicit_local_daemons_are_accepted(volumes: WorkerVolumes, host: str) -> None:
    with pytest.raises(ValidationError):
        WorkerVolumeSettings.model_validate({**volumes.settings.model_dump(), "docker_host": host})


@pytest.mark.parametrize("path", ["docker", "/a/../docker"])
def test_executable_selection_is_explicit(volumes: WorkerVolumes, path: str) -> None:
    with pytest.raises(ValidationError):
        WorkerVolumeSettings.model_validate({**volumes.settings.model_dump(), "docker_cli": path})


def test_cli_does_not_inherit_ambient_authority(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "other")
    monkeypatch.setenv("SYNTHETIC_SECRET", "not-for-staging")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"native-result", b""))
    monkeypatch.setattr(subprocess, "run", run)
    assert volumes.command("version") == b"native-result"
    args, kwargs = run.call_args
    assert args[0][:2] == ["/opt/trusted/docker", "--config"]
    assert args[0][3:] == ["--host", "unix:///var/run/docker.sock", "version"]
    assert not Path(args[0][2]).exists()
    assert kwargs["env"] == {"PATH": os.defpath}
    assert kwargs["timeout"] == 60


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess([], 1, b"private", b"private"),
        subprocess.CompletedProcess([], 0, b"x" * (1024 * 1024 + 1), b""),
        subprocess.TimeoutExpired("private", 60),
        OSError("private"),
    ],
)
def test_native_failure_is_sanitized_and_never_retried(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
) -> None:
    run = (
        Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    )
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(WorkerAttemptError, match="reconciliation") as error:
        volumes.command("version")
    assert "private" not in str(error.value)
    assert run.call_count == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "owner",
        "name",
        "image",
        "network",
        "rw",
        "volume",
        "bind",
        "shadow",
        "ancestor",
        "missing",
        "malformed",
        "config",
        "host",
    ],
)
def test_worker_inspection_fails_closed_on_changed_admission(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    observed = _container(volumes)
    name = observed["Name"].removeprefix("/")
    if mutation == "owner":
        observed["Config"]["Labels"]["heartwood.attempt"] = str(uuid4())
    elif mutation == "name":
        observed["Name"] = "/replacement"
    elif mutation == "image":
        observed["Image"] = "sha256:" + "f" * 64
    elif mutation == "network":
        observed["HostConfig"]["NetworkMode"] = "host"
    elif mutation in {"rw", "volume", "bind"}:
        key, value = {"rw": ("RW", True), "volume": ("Name", "foreign"), "bind": ("Type", "bind")}[
            mutation
        ]
        observed["Mounts"][0][key] = value
    elif mutation in {"shadow", "ancestor"}:
        observed["Mounts"].append(
            {"Destination": "/inputs/hidden" if mutation == "shadow" else "/"}
        )
    elif mutation == "missing":
        observed["Mounts"] = []
    elif mutation == "malformed":
        observed["Mounts"] = [None]
    elif mutation == "config":
        observed["Config"] = None
    else:
        observed["HostConfig"] = None
    command = Mock(return_value=json.dumps([observed]).encode())
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError):
        volumes.inspect_worker(name)
    assert command.call_count == 1


def test_worker_inspection_accepts_exact_read_only_projections(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _container(volumes)
    monkeypatch.setattr(volumes, "command", Mock(return_value=json.dumps([observed]).encode()))
    volumes.inspect_worker(observed["Name"].removeprefix("/"))
    assert len(volumes.mounts()) == 2
    assert all(mount["read_only"] is True for mount in volumes.mounts())
    with pytest.raises(WorkerAttemptError, match="name"):
        volumes.inspect_worker("another-worker")


@pytest.mark.parametrize("response", [b"null", b"{}", b"[{},{}]", b"invalid", b"\xff"])
def test_malformed_native_inspection_never_authorizes_cleanup(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
    response: bytes,
) -> None:
    command = Mock(return_value=response)
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError):
        volumes.inspect_worker(f"openshell-synthetic--{volumes.worker_name}-native-id")
    assert all("rm" not in call.args for call in command.call_args_list)


@pytest.mark.parametrize("by_name", [False, True])
def test_any_remaining_worker_blocks_projection_removal(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
    by_name: bool,
) -> None:
    command = Mock(side_effect=[b"", b"orphan"] if by_name else [b"worker"])
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError, match="remains"):
        volumes.remove_after_revocation()
    assert all("rm" not in call.args for call in command.call_args_list)


def test_cleanup_refuses_foreign_volume_and_preserves_it(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = volumes.names["inputs"]
    command = Mock(side_effect=[b"", b"", b"", b"", name.encode(), b'[{"Labels": {}}]'])
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError, match="belong"):
        volumes.remove_after_revocation()
    assert all("rm" not in call.args for call in command.call_args_list)


def test_cleanup_checks_staging_name_even_when_ownership_labels_disappear(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = Mock(side_effect=[b"", b"", b"", b"orphan", b'[{"Config": {"Labels": {}}}]'])
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError, match="belong"):
        volumes.remove_after_revocation()
    assert all("rm" not in call.args for call in command.call_args_list)


def test_cleanup_absence_is_idempotent(
    volumes: WorkerVolumes, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Mock(return_value=b"")
    monkeypatch.setattr(volumes, "command", command)
    volumes.remove_after_revocation()
    volumes.remove_after_revocation()
    assert all("rm" not in call.args for call in command.call_args_list)


def test_partial_staging_failure_requires_cleanup_without_repeating_transfer(
    volumes: WorkerVolumes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {name: tmp_path / name for name in volumes.names}
    for source in sources.values():
        source.mkdir(mode=0o700)
    labels = {**volumes.labels, "heartwood.role": "projection"}
    command = Mock(
        side_effect=[
            b"",
            b"created",
            json.dumps([{"Labels": labels}]).encode(),
            WorkerAttemptError("uncertain second volume"),
        ]
    )
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError, match="uncertain"):
        volumes.stage(sources)
    calls = command.call_count
    with pytest.raises(WorkerAttemptError, match="cannot repeat"):
        volumes.stage(sources)
    assert command.call_count == calls


def test_existing_projection_is_never_overwritten(
    volumes: WorkerVolumes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = {name: tmp_path / name for name in volumes.names}
    for source in sources.values():
        source.mkdir(mode=0o700)
    command = Mock(return_value=volumes.names["inputs"].encode())
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError, match="already exists"):
        volumes.stage(sources)
    assert command.call_count == 1


@pytest.mark.parametrize("failure", [None, "identity", "running", "exit", "owner"])
def test_transfer_observes_helper_completion_before_removing_it(
    volumes: WorkerVolumes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    sources = {name: tmp_path / name for name in volumes.names}
    for source in sources.values():
        source.mkdir(mode=0o700)
    helper = _container(volumes, role="staging")
    finished = json.loads(json.dumps(helper))
    if failure == "identity":
        finished["Id"] = "replacement"
    elif failure == "running":
        finished["State"]["Running"] = True
    elif failure == "exit":
        finished["State"]["ExitCode"] = 1
    elif failure == "owner":
        finished["Config"]["Labels"] = {}
    labels = {**volumes.labels, "heartwood.role": "projection"}
    replies = []
    for name in volumes.names.values():
        replies.extend(
            [b"", name.encode(), json.dumps([{"Name": name, "Labels": labels}]).encode()]
        )
    replies.extend(
        [
            b"native-id",
            json.dumps([helper]).encode(),
            b"",
            b"",
            b"",
            json.dumps([finished]).encode(),
        ]
    )
    if failure is None:
        replies.extend([b"native-id", b"native-id", json.dumps([finished]).encode(), b"removed"])
    command = Mock(side_effect=replies)
    monkeypatch.setattr(volumes, "command", command)
    if failure is None:
        volumes.stage(sources)
        assert command.call_args.args == ("container", "rm", "--force", "native-id")
    else:
        with pytest.raises(WorkerAttemptError):
            volumes.stage(sources)
        assert all("rm" not in call.args for call in command.call_args_list)
    create = next(
        call.args for call in command.call_args_list if call.args[:2] == ("container", "create")
    )
    assert "--network=none" in create
    assert "--read-only" in create
    assert "--pull=never" in create
    assert "--cap-drop=ALL" in create
    assert "--cap-add=FOWNER" in create
    assert "--security-opt=no-new-privileges" in create
    assert not any("type=bind" in arg for arg in create)
    assert _IMAGE in create


@pytest.mark.parametrize("failure", ["missing", "shared", "symlink", "relative"])
def test_staging_rejects_unprotected_or_incomplete_snapshots_before_native_calls(
    volumes: WorkerVolumes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sources = {name: tmp_path / name for name in volumes.names}
    for source in sources.values():
        source.mkdir(mode=0o700)
    if failure == "missing":
        sources.pop("skills")
    elif failure == "shared":
        sources["inputs"].chmod(0o755)
    elif failure == "relative":
        sources["inputs"] = Path("relative")
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(sources["inputs"], target_is_directory=True)
        sources["inputs"] = alias
    command = Mock()
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError):
        volumes.stage(sources)
    command.assert_not_called()


def test_cleanup_removes_only_observed_owned_volumes(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [b"", b"", b"", b""]
    for name in volumes.names.values():
        replies.extend(
            [
                name.encode(),
                json.dumps(
                    [{"Name": name, "Labels": {**volumes.labels, "heartwood.role": "projection"}}]
                ).encode(),
                b"removed",
            ]
        )
    command = Mock(side_effect=replies)
    monkeypatch.setattr(volumes, "command", command)
    volumes.remove_after_revocation()
    assert [call.args for call in command.call_args_list if "rm" in call.args] == [
        ("volume", "rm", name) for name in volumes.names.values()
    ]


def test_ambiguous_volume_listing_prevents_removal(
    volumes: WorkerVolumes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = Mock(side_effect=[b"", b"", b"", b"", b"unexpected multiple"])
    monkeypatch.setattr(volumes, "command", command)
    with pytest.raises(WorkerAttemptError, match="ambiguous"):
        volumes.remove_after_revocation()
    assert all("rm" not in call.args for call in command.call_args_list)


def test_staging_image_must_match_retained_intent(volumes: WorkerVolumes) -> None:
    intent = WorkerAttemptIntent(
        project_id=uuid4(),
        session_id="synthetic",
        attempt_id=uuid4(),
        conversation_id=uuid4(),
        image_digest="sha256:" + "f" * 64,
        gateway_policy_sha256="b" * 64,
        sandbox_policy_sha256="c" * 64,
        input_manifest_sha256="d" * 64,
    )
    with pytest.raises(WorkerAttemptError, match="image"):
        WorkerVolumes(volumes.settings, intent)
