# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Real-model qualification never accepts credentials in its public profile."""

from __future__ import annotations

import importlib
import io
import json
import subprocess
import sys
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


def test_model_helpers_import_without_native_sdk() -> None:
    root = Path(__file__).resolve().parents[3]
    program = """
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / "deploy"), str(root / "deploy/tests")]
sys.modules["openshell"] = None
from test_worker_model_integration import allowed_action

assert allowed_action("terminal", {"command": "cat /inputs/summary.json"}) == "inspect-input"
assert allowed_action("terminal", {"command": "cat /inputs/summary.json; env"}) is None
"""
    subprocess.run([sys.executable, "-c", program, str(root)], check=True, timeout=60)


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
    for command, identity in (
        ("cat /inputs/summary.json", "inspect-input"),
        (runner.TERMINAL_COMMAND, "aggregate-output"),
    ):
        assert (
            runner.allowed_action(
                "terminal", {"command": command, "is_input": False, "reset": False, "timeout": None}
            )
            == identity
        )
        for argument, value in (
            ("is_input", True),
            ("is_input", 0),
            ("reset", 0),
            ("timeout", 0),
            ("timeout", 30),
            ("extra", None),
        ):
            assert runner.allowed_action("terminal", {"command": command, argument: value}) is None
    diagnostic = runner.action_diagnostic("private-tool", {"private-key": "private-value"})
    assert "private" not in json.dumps(diagnostic)
    assert diagnostic["recognized_identity"] is None
    assert (
        len(runner.action_diagnostic("terminal", {str(i): i for i in range(20)})["arguments"]) == 8
    )


@pytest.mark.usefixtures("profile_module")
def test_approval_accepts_only_typed_advisory_metadata_from_sdk_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openhands.sdk.event import ActionEvent
    from openhands.sdk.llm import MessageToolCall
    from openhands.sdk.security import SecurityRisk
    from openhands.sdk.tool.builtins.invoke_skill import InvokeSkillAction
    from openhands.tools.terminal import TerminalAction

    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    runner = importlib.import_module("test_worker_model_integration")
    for tool, action, executable, identity in (
        (
            "terminal",
            TerminalAction(command="cat /inputs/summary.json"),
            {"command": "cat /inputs/summary.json", "reset": False},
            "inspect-input",
        ),
        (
            "invoke_skill",
            InvokeSkillAction(name="aggregate-export"),
            {"name": "aggregate-export"},
            "invoke-skill",
        ),
    ):
        assert {"summary", "security_risk"}.isdisjoint(type(action).model_fields)
        for risk in SecurityRisk:
            raw = {**executable, "security_risk": risk.value, "summary": "Synthetic operation"}
            event = ActionEvent(
                action=action,
                thought=[],
                tool_name=tool,
                tool_call_id="synthetic-call",
                tool_call=MessageToolCall(
                    id="synthetic-call", name=tool, arguments=json.dumps(raw), origin="completion"
                ),
                llm_response_id="synthetic-response",
                security_risk=risk,
                summary="Synthetic operation",
            )
            arguments = dict(runner._openhands_sdk._tool_arguments(event))
            assert arguments == raw
            assert runner.allowed_action(tool, arguments) == identity
        invalid_metadata: tuple[dict[str, object], ...] = (
            {"summary": None},
            {"summary": 1},
            {"summary": "x" * 1025},
            {"security_risk": None},
            {"security_risk": []},
            {"security_risk": "INVALID"},
            {"unknown": "metadata"},
        )
        for metadata in invalid_metadata:
            assert runner.allowed_action(tool, {**executable, **metadata}) is None
        assert runner.allowed_action(tool, {**executable, "summary": "x" * 1024}) == identity
    assert (
        runner.allowed_action(
            "terminal",
            {"command": "cat /inputs/summary.json; env", "summary": "Safe", "security_risk": "LOW"},
        )
        is None
    )


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
@pytest.mark.parametrize(("lifetime", "update_fails"), [(None, False), (30, False), (30, True)])
def test_native_provider_expiry_is_installed_before_worker_admission(
    monkeypatch: pytest.MonkeyPatch, lifetime: int | None, update_fails: bool
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

    def invoke(arguments: list[str], **environment: str) -> str:
        assert "synthetic-qualification-secret" not in arguments
        if arguments[1] == "create":
            assert "--credential-expires-at" not in arguments
            assert fixture.resources.callback.call_count == 0
            if lifetime is not None:
                assert environment["HEARTWOOD_QUALIFICATION_KEY"] == "qualification-pending"
        else:
            assert arguments[:3] == ["provider", "update", "synthetic-owner"]
            fixture.resources.callback.assert_called_once_with(
                fixture.cli_command, ["provider", "delete", "synthetic-owner"]
            )
            if update_fails:
                raise RuntimeError("Synthetic provider update failed")
        return ""

    fixture.cli_command.side_effect = invoke
    if update_fails:
        with pytest.raises(RuntimeError, match="Synthetic provider update failed"):
            fixture._create_inference_provider()
    else:
        fixture._create_inference_provider()
    arguments = fixture.cli_command.call_args.args[0]
    assert fixture.cli_command.call_args.kwargs == {
        "HEARTWOOD_QUALIFICATION_KEY": "synthetic-qualification-secret"
    }
    assert "synthetic-qualification-secret" not in arguments
    if lifetime is None:
        assert fixture.cli_command.call_count == 1
        assert "--credential-expires-at" not in arguments
        assert fixture.inference_deadline is None
    else:
        assert fixture.cli_command.call_count == 2
        assert arguments[:3] == ["provider", "update", "synthetic-owner"]
        assert arguments[-2] == "--credential-expires-at"
        assert arguments[-1] == "HEARTWOOD_QUALIFICATION_KEY=" + str(130000)
        assert fixture.inference_deadline == 40.0
        assert fixture.credential_expires_at_ms == 130000
    fixture.resources.callback.assert_called_once_with(
        fixture.cli_command, ["provider", "delete", "synthetic-owner"]
    )


@pytest.mark.usefixtures("profile_module")
@pytest.mark.parametrize("failure", [None, "expiry", "status", "code", "placeholder", "attachment"])
def test_model_revocation_preserves_key_and_requires_exact_native_denial(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    module = importlib.import_module("test_worker_model_integration")
    fixture = MagicMock()
    fixture.identity = "synthetic-owner"
    fixture.credential_expires_at_ms = 123456
    fixture.model_profile.model = "openai/synthetic-model"
    fixture.control.get.return_value = SimpleNamespace(id="same-worker")
    steps: list[str] = []

    def cli(arguments: list[str], **environment: str) -> str:
        assert environment == {}
        if arguments[:3] == ["sandbox", "provider", "list"]:
            names = [] if "detach" in steps else [fixture.identity]
            if failure == "attachment" and "detach" in steps:
                names = [fixture.identity]
            return json.dumps(
                {"providers": [{"name": name} for name in names], "next_page_token": ""}
            )
        assert arguments == ["sandbox", "provider", "detach", "worker", fixture.identity]
        steps.append("detach")
        return ""

    def probe(worker: str, *, body: dict[str, object] | None = None) -> dict[str, object]:
        assert worker == "worker"
        if body is None:
            steps.append("hash")
            return {"placeholder_sha256": "same-hash"}
        assert steps == ["hash", "expiry"]
        assert body == {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": "Synthetic revocation check."}],
            "max_tokens": 1,
        }
        steps.append("probe")
        return {
            "status": 403 if failure == "status" else 500,
            "error_code": "different" if failure == "code" else "credential_unavailable",
            "placeholder_sha256": "different" if failure == "placeholder" else "same-hash",
        }

    def wait_for_expiry() -> None:
        steps.append("expiry")
        if failure == "expiry":
            raise TimeoutError("synthetic expiry timeout")

    fixture.cli_command.side_effect = cli
    fixture.probe_inference.side_effect = probe
    fixture.wait_for_inference_expiry.side_effect = wait_for_expiry
    if failure is None:
        result = module.revoke_model_provider(fixture, "worker")
        assert result["provider_detached"] is True
        assert result["credential_expires_at_ms"] == fixture.credential_expires_at_ms
        assert steps == ["hash", "expiry", "probe", "detach"]
    else:
        with pytest.raises(TimeoutError if failure == "expiry" else AssertionError):
            module.revoke_model_provider(fixture, "worker")
        if failure in {"expiry", "status", "code", "placeholder"}:
            assert "detach" not in steps
        if failure == "expiry":
            assert "probe" not in steps


@pytest.mark.usefixtures("profile_module")
@pytest.mark.parametrize("case", ["hash", "denied", "oversized", "transport", "bad-code"])
def test_shared_inference_probe_has_bounded_content_safe_output(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import contextlib
    import sys
    import urllib.error
    import urllib.request
    from email.message import Message

    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    module = importlib.import_module("worker_native_fixture")
    fixture = module.NativeFixture.__new__(module.NativeFixture)
    fixture.base_url = "https://provider.invalid/v1"
    fixture.control = MagicMock()
    placeholder = "openshell:resolve:env:synthetic-placeholder"
    monkeypatch.setenv("HEARTWOOD_QUALIFICATION_KEY", placeholder)
    payload = {"model": "synthetic-model", "messages": [{"role": "user", "content": "synthetic"}]}

    def request(request: urllib.request.Request, *, timeout: int) -> None:
        assert case != "hash"
        assert timeout == 10
        assert request.full_url == fixture.base_url + "/chat/completions"
        assert isinstance(request.data, bytes)
        assert json.loads(request.data) == payload
        if case == "transport":
            raise urllib.error.URLError("synthetic DNS failure")
        content = json.dumps({"error": "credential_unavailable"}).encode()
        if case == "oversized":
            content = b"x" * 16385
        if case == "bad-code":
            content = json.dumps({"error": "x" * 129}).encode()
        raise urllib.error.HTTPError(
            request.full_url, 500, "synthetic", Message(), io.BytesIO(content)
        )

    monkeypatch.setattr(urllib.request, "urlopen", request)

    def execute(worker: str, arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        assert worker == "worker"
        assert placeholder not in json.dumps(arguments)
        monkeypatch.setattr(sys, "argv", ["-c", *arguments[4:]])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(arguments[3], "native-inference-probe", "exec"), {})
        assert placeholder not in output.getvalue()
        return SimpleNamespace(exit_code=0, stdout=output.getvalue())

    fixture.control.exec.side_effect = execute
    if case in {"oversized", "bad-code", "transport"}:
        with pytest.raises((AssertionError, urllib.error.URLError)):
            fixture.probe_inference("worker", body=payload)
    else:
        result = fixture.probe_inference("worker", body=None if case == "hash" else payload)
        assert len(result["placeholder_sha256"]) == 64
        if case == "denied":
            assert result["status"] == 500
            assert result["error_code"] == "credential_unavailable"
        else:
            assert set(result) == {"placeholder_sha256"}


@pytest.mark.usefixtures("profile_module")
@pytest.mark.parametrize("mode", ["already-expired", "advance", "stalled", "backward"])
def test_native_expiry_wait_requires_elapsed_wallclock_and_is_monotonic_bounded(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    module = importlib.import_module("worker_native_fixture")
    fixture = module.NativeFixture.__new__(module.NativeFixture)
    fixture.credential_expires_at_ms = 10000
    fixture.inference_deadline = 100.0
    clock = [10.2 if mode == "already-expired" else 10.0, 100.0]
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[1])

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[1] += 1.0
        if mode == "advance":
            clock[0] += 0.2
        elif mode == "backward":
            clock[0] -= 1.0

    monkeypatch.setattr(module.time, "sleep", sleep)
    if mode in {"stalled", "backward"}:
        with pytest.raises(TimeoutError, match="expiry could not be established"):
            fixture.wait_for_inference_expiry()
        assert len(sleeps) == 3
    else:
        fixture.wait_for_inference_expiry()
        assert len(sleeps) == (0 if mode == "already-expired" else 1)


@pytest.mark.usefixtures("profile_module")
@pytest.mark.parametrize(
    ("base_url", "hostname"),
    [
        ("https://provider.invalid/v1", "provider.invalid"),
        ("https://host.docker.internal:19671/v1", "host.docker.internal"),
    ],
)
def test_direct_network_probe_uses_the_actual_provider_hostname(
    monkeypatch: pytest.MonkeyPatch, base_url: str, hostname: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "deploy/tests"))
    module = importlib.import_module("worker_native_fixture")
    fixture = module.NativeFixture.__new__(module.NativeFixture)
    fixture.base_url = base_url
    fixture.control = MagicMock()
    fixture.ipv4, fixture.ipv6 = "192.0.2.1", "2001:db8::1"
    qualify = MagicMock(return_value={"passed": True})
    monkeypatch.setattr(module, "qualify_network", qualify)
    assert fixture.qualify_direct_network("worker") == {"passed": True}
    assert qualify.call_args.kwargs["allowed_host"] == hostname
    assert qualify.call_args.args == (fixture.control, "worker")
