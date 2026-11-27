# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Offline checks for the hosted failure-case observations and fixture ownership."""

import json
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from heartwood_platform.synthetic_model import _interruption
from hub import Browser, Server
from kube import Cluster
from model_denial import model_denied
from test_hosted_worker import (
    _CaseServer,
    _owned_server,
    _require_interruption,
    _require_model_denial,
    _revoked_turn,
    _settled_failure,
)


def _event(kind: str, **payload: str) -> dict[str, Any]:
    return {"session_id": "case", "kind": kind, "payload": payload}


def test_revocation_checks_all_diagnostics_in_the_terminal_snapshot() -> None:
    early = [
        _event("worker_attempt.recorded", phase="active"),
        _event("agent.lifecycle.updated", status="running"),
        _event("error.recorded", code="HW-AGENT-011"),
    ]
    terminal = _event("agent.lifecycle.updated", status="error")
    assert not _revoked_turn(early, "case")
    assert _revoked_turn([*early, terminal], "case")
    transient = _event("error.recorded", code="HW-AGENT-012", detail="private fixture text")
    for snapshot in (
        [*early, transient, terminal],
        [*early, terminal, transient],
        [*early[:-1], transient, terminal],
    ):
        with pytest.raises(
            AssertionError, match=r"^Revoked model produced the wrong terminal diagnostic$"
        ):
            _revoked_turn(snapshot, "case")
    with pytest.raises(AssertionError, match=r"^Unexpected conversation in the event stream$"):
        _revoked_turn([*early, dict(terminal, session_id="unrelated")], "case")


@pytest.mark.parametrize(
    "kind",
    [
        "tool_call.proposed",
        "confirmation.requested",
        "tool.execution.recorded",
        "agent_message.emitted",
    ],
)
def test_terminal_failure_rejects_tool_activity_and_synthetic_success(kind: str) -> None:
    events = [
        _event("worker_attempt.recorded", phase="active"),
        _event("error.recorded", code="HW-AGENT-011"),
        _event(kind, text="private fixture text: synthetic model route completed"),
        _event("agent.lifecycle.updated", status="error"),
    ]
    with pytest.raises(
        AssertionError,
        match=r"^A failed turn (requested or executed a tool|reported successful completion)$",
    ):
        _revoked_turn(events, "case")


def test_interruption_requires_the_receipt_for_the_submitted_request() -> None:
    nonce = "1" * 32
    prompt = f"private fixture text synthetic:interrupt:{nonce}"
    receipt = _interruption([{"role": "user", "content": [{"text": prompt}]}])
    assert receipt
    assert "private" not in receipt
    assert nonce not in receipt
    log = f"unrelated private fixture log\n_InterruptedAnswerError: {receipt}"
    _require_interruption(log, nonce)
    with pytest.raises(
        AssertionError, match=r"^Missing backend interruption for the submitted request$"
    ):
        _require_interruption(log, "2" * 32)


def test_existing_named_server_is_preserved() -> None:
    server = Mock(spec=_CaseServer)
    server.registration.return_value = {"user_options": {}}
    with pytest.raises(AssertionError, match="already registered"), _owned_server(server):
        pytest.fail("Unexpected fixture entry")
    server.start.assert_not_called()
    server.remove.assert_not_called()


@pytest.mark.parametrize(
    ("options", "owned"),
    [(None, False), ({}, False), ({"test_owner": "other"}, False), ({"test_owner": "ours"}, True)],
)
def test_failed_start_removes_only_its_own_registration(
    options: dict[str, str] | None, owned: bool
) -> None:
    server = Mock(spec=_CaseServer)
    server.session = ""
    server.registered_session.side_effect = ["", "0123456789abcdef"]
    server.owner = "ours"
    server.registration.side_effect = [
        None,
        {"user_options": options} if options is not None else None,
    ]
    server.start.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"), _owned_server(server):
        pytest.fail("Unexpected fixture entry")
    assert server.remove.call_count == int(owned)
    if owned:
        assert server.session == "0123456789abcdef"


def test_pending_start_rechecks_ownership_before_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Mock(spec=_CaseServer)
    server.session = ""
    server.registered_session.side_effect = ["", "0123456789abcdef"]
    server.owner = "ours"
    server.registration.side_effect = [
        None,
        {"pending": "spawn", "user_options": {"test_owner": "ours"}},
        {"user_options": {"test_owner": "replacement"}},
    ]
    monkeypatch.setattr("test_hosted_worker.time.sleep", lambda _: None)
    with _owned_server(server):
        pass
    server.remove.assert_not_called()


def test_wait_timeout_does_not_render_the_journal() -> None:
    server = Mock(spec=_CaseServer)
    server.events.return_value = [_event("agent_message.emitted", text="private fixture text")]
    with pytest.raises(
        AssertionError, match=r"^Timed out waiting for the conversation observation$"
    ):
        _CaseServer.wait(server, lambda _: False, timeout=0)
    server.events.assert_not_called()


def test_late_diagnostic_is_read_after_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Mock(spec=Server)
    server.conversation = "case"
    first = [
        _event("worker_attempt.recorded", phase="active"),
        _event("agent.lifecycle.updated", status="error"),
        _event("error.recorded", code="HW-AGENT-011"),
    ]
    final = [*first, _event("error.recorded", code="HW-AGENT-012")]
    server.wait.side_effect = lambda check: check(first)
    server.api.side_effect = [
        (409, {"error": "session execution is still settling"}),
        (200, {"events": final}),
    ]
    monkeypatch.setattr("test_hosted_worker.time.sleep", lambda _: None)
    with pytest.raises(AssertionError, match="wrong terminal diagnostic"):
        _revoked_turn(_settled_failure(server), "case")
    assert server.api.call_count == 2
    server.api.assert_called_with("GET", "/sessions/case/events?settled=true")


def test_denial_requires_linked_native_event_in_exact_conversation(tmp_path: Path) -> None:
    native = uuid4()
    linked, unrelated = str(uuid4()), str(uuid4())
    events = tmp_path / native.hex / "events"
    events.mkdir(parents=True)

    def write(source: str, detail: str) -> None:
        (events / f"event-00012-{source}.json").write_text(
            json.dumps(
                {
                    "kind": "ConversationErrorEvent",
                    "source": "environment",
                    "id": source,
                    "code": "APIError",
                    "detail": detail,
                }
            )
        )

    model = "synthetic-coder"
    denial = f"The requested model '{model}' is not available for this API key"
    write(unrelated, denial)
    write(linked, "other configuration error")
    assert not model_denied(tmp_path, str(native), model, [linked])
    write(linked, denial)
    assert not model_denied(tmp_path, str(uuid4()), model, [linked])
    assert model_denied(tmp_path, str(native), model, [linked])


def test_cleanup_waits_for_worker_namespace_after_gateway_objects_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = Mock(spec=Cluster)
    browser = Mock(spec=Browser, cluster=cluster)
    browser.hub_api.return_value = (200, {"servers": {}})
    monkeypatch.setattr("hub.researcher", lambda _: "test-researcher")
    server = Server(browser, "user", "other", session="abc")
    remaining = iter(["namespace/hw-s-abc", ""])

    def get(*args: str) -> CompletedProcess[str]:
        output = next(remaining) if "namespace/hw-s-abc" in args else ""
        return CompletedProcess([], 0, output, "")

    cluster.kubectl.side_effect = get
    sleep = Mock()
    monkeypatch.setattr("hub.time.sleep", sleep)
    server.remove()
    sleep.assert_called_once()


def test_hub_api_keeps_header_in_a_scoped_private_file(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Mock(spec=Cluster)
    browser = Browser(cluster, "probe", "user")
    monkeypatch.setattr(Browser, "cookie", lambda *_: "synthetic")
    header = "X-XSRFToken: synthetic"
    cluster.kubectl.side_effect = [
        CompletedProcess([], 0, str(len(header)), ""),
        CompletedProcess([], 0, "{}\n200", ""),
        CompletedProcess([], 0, "", ""),
    ]
    assert browser.hub_api("GET", "/users/user") == (200, {})
    write, request, cleanup = cluster.kubectl.call_args_list
    assert write.kwargs == {"text_input": header}
    assert header not in request.args
    assert "@" + cleanup.args[-1] in request.args
    assert cleanup.args[-3:-1] == ("rm", "-f")


def test_denial_probe_requires_receipt_from_completed_script() -> None:
    cluster = Mock(spec=Cluster)
    server = Server(
        Browser(cluster, "probe", "user"),
        "user",
        "other",
        session="0123456789abcdef",
        conversation="case",
    )
    attempt, native, source = (str(uuid4()) for _ in range(3))
    events = [
        dict(
            _event("worker_attempt.recorded"),
            payload={
                "phase": "active",
                "intent": {
                    "attempt_id": attempt,
                    "conversation_id": native,
                    "session_id": "case",
                },
            },
        ),
        _event("error.recorded", code="HW-AGENT-011", source_event_id=f"openhands:{source}:error"),
        _event("agent.lifecycle.updated", status="error"),
    ]
    pods = CompletedProcess([], 0, "pod/worker\n", "")
    cluster.kubectl.side_effect = [
        pods,
        CompletedProcess([], 0, "", ""),
        pods,
        CompletedProcess([], 0, "model-access-denial-verified\n", ""),
    ]
    with pytest.raises(AssertionError, match="Missing model-access denial"):
        _require_model_denial(server, events)
    _require_model_denial(server, events)
    select, execute = cluster.kubectl.call_args_list[-2:]
    assert f"--selector=heartwood.role=worker,heartwood.attempt={attempt}" in select.args
    assert execute.args[-3:] == (native, "synthetic-coder", source)
