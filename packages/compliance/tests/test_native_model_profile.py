# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Real-model qualification never accepts credentials in its public profile."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


@pytest.fixture
def profile_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "deploy"))
    return importlib.import_module("worker_model_profile")


@pytest.mark.parametrize(
    "changed",
    [
        {"api_key": "synthetic-secret"},
        {"base_url": "http://provider.invalid/v1"},
        {"base_url": "https://secret@provider.invalid/v1"},
        {"base_url": "https://provider.invalid/v1?key=synthetic-secret"},
        {"base_url": "https://provider.invalid/v1/../other"},
        {"max_iterations": 0},
        {"max_iterations": 100},
        {"max_iterations": True},
        {"deadline_seconds": 601},
        {"max_output_tokens": 100000},
        {"model": "openai/unsafe\nmodel"},
    ],
)
def test_profile_rejects_inline_credentials_and_unbounded_routes(
    profile_module: ModuleType, tmp_path: Path, changed: dict[str, object]
) -> None:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"base_url": "https://provider.invalid/v1", "model": "openai/test", **changed})
    )
    with pytest.raises(ValueError, match="credentials belong on stdin") as failure:
        profile_module.QualificationModelProfile.load(path)
    assert "synthetic-secret" not in str(failure.value)


def test_profile_round_trip_is_non_secret(profile_module: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text('{"base_url":"https://provider.invalid/v1","model":"openai/test"}')
    profile = profile_module.QualificationModelProfile.load(path)
    assert profile.endpoint == "https://provider.invalid/v1/chat/completions"
    assert profile.mapping() == {
        "base_url": "https://provider.invalid/v1",
        "model": "openai/test",
        "max_input_tokens": 32768,
        "max_output_tokens": 512,
        "max_iterations": 6,
        "deadline_seconds": 180,
    }


@pytest.mark.parametrize(
    "value", [b"", b"with whitespace\n", b"x" * 16385, b"\xff", b"nul\x00", b"escape\x1b"]
)
def test_invalid_credential_never_appears_in_an_error(
    profile_module: ModuleType, value: bytes
) -> None:
    with pytest.raises(ValueError, match="unavailable or invalid"):
        profile_module.read_credential(io.BytesIO(value))


def test_credential_is_read_once_and_bounded(profile_module: ModuleType) -> None:
    stream = io.BytesIO(b"synthetic-secret\nunused-line\n")
    assert profile_module.read_credential(stream) == "synthetic-secret"
    assert stream.read() == b"unused-line\n"


@pytest.mark.usefixtures("profile_module")
def test_approval_allowlist_rejects_extra_arguments_and_combined_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    runner = importlib.import_module("test_worker_model_integration")
    assert (
        runner.allowed_action("terminal", {"command": "cat /inputs/summary.json"})
        == "inspect-input"
    )
    assert runner.allowed_action("terminal", {"command": "cat /inputs/summary.json; env"}) is None
    assert (
        runner.allowed_action("terminal", {"command": "cat /inputs/summary.json", "reset": True})
        is None
    )
    assert runner.allowed_action("invoke_skill", {"name": "aggregate-export"}) == "invoke-skill"
    assert runner.allowed_action("invoke_skill", {"name": "unreviewed"}) is None


@pytest.mark.usefixtures("profile_module")
@pytest.mark.parametrize("failure", [None, "detach", "identity", "pending"])
def test_deadline_uses_native_ownership_without_waiting_for_application_startup(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    runner = importlib.import_module("test_worker_model_integration")
    project_id, attempt_id = uuid4(), uuid4()
    name = "hw-" + attempt_id.hex[:16]
    worker = SimpleNamespace(
        id="bound-worker",
        name=name,
        labels={
            "heartwood.project": str(project_id),
            "heartwood.attempt": str(attempt_id),
        },
    )
    fixture = MagicMock()
    fixture.identity = "qualification-provider"
    fixture.control.list_all.return_value = [] if failure == "pending" else [worker]
    fixture.control.get.return_value = SimpleNamespace(
        id="replacement" if failure == "identity" else "bound-worker",
        labels=worker.labels,
    )
    if failure == "detach":
        fixture.cli_command.side_effect = RuntimeError("synthetic secret must not appear")
    errors = runner.revoke_after_deadline(fixture, project_id)
    fixture.control.list_all.assert_called_once_with(
        workspace="default",
        label_selector=f"heartwood.project={project_id}",
    )
    if failure in {"pending", "identity"}:
        fixture.cli_command.assert_not_called()
        fixture.control.delete.assert_not_called()
        assert errors == (["RuntimeError"] if failure == "identity" else [])
    else:
        fixture.cli_command.assert_called_once_with(
            ["sandbox", "provider", "detach", name, fixture.identity]
        )
        fixture.control.delete.assert_called_once_with(
            name, workspace="default", timeout_seconds=15
        )
        assert errors == (["RuntimeError"] if failure == "detach" else [])


@pytest.mark.usefixtures("profile_module")
def test_gateway_log_leak_is_redacted_and_fails_qualification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    module = importlib.import_module("worker_native_fixture")
    fixture = module.NativeFixture.__new__(module.NativeFixture)
    fixture.identity = "synthetic-owner"
    fixture.output = tmp_path
    fixture._inference_credential = "synthetic-qualification-secret"
    inspect = MagicMock(
        return_value=json.dumps(
            [
                {
                    "Id": "synthetic-container",
                    "Config": {"Labels": {"heartwood-qualification-owner": fixture.identity}},
                }
            ]
        )
    )
    monkeypatch.setattr(module, "command", inspect)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=fixture._inference_credential, stderr=""),
    )
    with pytest.raises(RuntimeError, match="protected qualification content") as failure:
        fixture._stop_gateway()
    assert fixture._inference_credential not in str(failure.value)
    assert (tmp_path / "gateway.log").read_text() == "[redacted]"
    assert inspect.call_count == 1


@pytest.mark.usefixtures("profile_module")
@pytest.mark.parametrize("lifetime", [None, 30])
def test_native_provider_expiry_is_installed_before_worker_admission(
    monkeypatch: pytest.MonkeyPatch, lifetime: int | None
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    module = importlib.import_module("worker_native_fixture")
    fixture = module.NativeFixture.__new__(module.NativeFixture)
    fixture.identity = "synthetic-owner"
    fixture.provider_deadline_seconds = lifetime
    fixture.inference_deadline = None
    fixture.credential_expires_at_ms = None
    fixture._inference_credential = "synthetic-qualification-secret"
    fixture.resources = MagicMock()
    fixture.cli_command = MagicMock()
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    fixture._create_inference_provider()
    arguments = fixture.cli_command.call_args.args[0]
    assert fixture.cli_command.call_args.kwargs == {
        "HEARTWOOD_QUALIFICATION_KEY": "synthetic-qualification-secret"
    }
    assert "synthetic-qualification-secret" not in arguments
    if lifetime is None:
        assert "--credential-expires-at" not in arguments
        assert fixture.inference_deadline is None
    else:
        assert arguments[-2:] == ["--credential-expires-at", "HEARTWOOD_QUALIFICATION_KEY=130000"]
        assert fixture.inference_deadline == 40.0
        assert fixture.credential_expires_at_ms == 130000
    fixture.resources.callback.assert_called_once_with(
        fixture.cli_command, ["provider", "delete", "synthetic-owner"]
    )
