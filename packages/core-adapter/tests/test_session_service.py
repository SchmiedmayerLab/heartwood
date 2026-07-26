# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import errno
import json
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import heartwood.core_adapter._state as session_state
from heartwood.audit import AuditIntegrityError, compute_event_hash
from heartwood.core_adapter import (
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendConfirmationResolutionEvent,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendEventSink,
    BackendTask,
    BackendTaskPlanEvent,
    BackendTaskStatus,
    BackendToolCallEvent,
    BackendUsage,
    BackendUsageEvent,
    CommandConflictError,
    DeterministicAgentBackend,
    FileSessionStore,
    LocalWorkspaceAgentBackend,
    PendingActionGroup,
    ProposedToolCall,
    SessionOwnershipError,
    SessionRecoveryError,
    SessionService,
    SessionStorageCapabilityError,
    SessionStoreBoundaryError,
    TokenDeltaSink,
    pending_action_group,
)
from heartwood.core_adapter._service import _audit_payload
from heartwood.schemas import AuditEvent, PolicyProfile
from heartwood.session import (
    CommandKind,
    EventKind,
    JsonValue,
    SessionCommand,
    SessionEvent,
    compute_session_event_hash,
)


def test_empty_replay_does_not_create_session_state(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    assert service.replay_events() == ()
    assert not service.store.session_dir.exists()


def test_reserved_audit_event_payloads_fail_closed() -> None:
    payload: dict[str, JsonValue] = {
        "unexpected_private_content": "synthetic-sensitive-value",
        "source_event_id": "source-1",
    }

    assert _audit_payload(EventKind.APPROVAL_RECORDED, payload) == {"action_count": 0}
    assert "synthetic-sensitive-value" not in json.dumps(
        _audit_payload(EventKind.POLICY_DECISION_RECORDED, payload)
    )


def test_pause_persists_replayable_events(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.PAUSE))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
    ]
    assert service.replay_events() == result.events


def test_pause_is_not_acknowledged_when_backend_does_not_reach_a_boundary(
    tmp_path: Path,
) -> None:
    service = SessionService.local_default(
        tmp_path,
        backend=_PauseFailureBackend(endpoint="https://model.local.invalid/v1/chat/completions"),
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.PAUSE).model_copy(update={"session_id": "session-main"})
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].payload["code"] == BackendErrorCode.WORKER_STOPPED.value


def test_completed_command_retry_returns_exact_events_without_backend_reexecution(
    tmp_path: Path,
) -> None:
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(BackendAgentMessageEvent(message="done"),),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    command = _command(CommandKind.CHAT, prompt="summarize").model_copy(
        update={"session_id": "session-main"}
    )

    first = service.handle(command)
    retried = service.handle(command)

    assert retried.replayed
    assert retried.events == first.events
    assert backend.prompts == ["summarize"]
    assert service.replay_events() == first.events


def test_completed_command_retry_survives_gateway_restart(tmp_path: Path) -> None:
    first_backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(BackendAgentMessageEvent(message="done"),),
    )
    first_service = SessionService.local_default(
        tmp_path,
        backend=first_backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    command = _command(CommandKind.CHAT, prompt="summarize").model_copy(
        update={"session_id": "session-main"}
    )
    first = first_service.handle(command)
    first_service.close()
    restarted_backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions"
    )
    restarted_service = SessionService.local_default(
        tmp_path,
        backend=restarted_backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    retried = restarted_service.handle(command)

    assert retried.events == first.events
    assert retried.replayed
    assert restarted_backend.prompts == []
    assert restarted_service.replay_events() == first.events


def test_command_identifier_reuse_with_different_content_is_rejected(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.PAUSE))

    with pytest.raises(CommandConflictError, match="different content"):
        service.handle(
            _command(CommandKind.PAUSE).model_copy(update={"created_at": "2026-01-01T00:00:01Z"})
        )

    assert len(service.replay_events()) == 2


def test_retried_approval_does_not_repeat_backend_tool_resolution(tmp_path: Path) -> None:
    tool_call = ProposedToolCall(
        tool_call_id="session-main-action",
        tool_name="file_editor",
        risk="medium",
        summary="write one file",
    )
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(
            BackendToolCallEvent(tool_call=tool_call),
            BackendConfirmationRequestEvent(
                tool_call=tool_call,
                action_group_id=_action_group_id(tool_call.tool_call_id),
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    service.handle(
        _command(CommandKind.CHAT, prompt="write").model_copy(update={"session_id": "session-main"})
    )
    approval = _command(
        CommandKind.APPROVE,
        target_type="action-set",
        target_id=_action_group_id(tool_call.tool_call_id),
    ).model_copy(update={"session_id": "session-main"})

    first = service.handle(approval)
    retried = service.handle(approval)

    assert retried.replayed
    assert retried.events == first.events
    assert backend.resolutions == [(_action_group_id(tool_call.tool_call_id), True)]


def test_second_writer_cannot_mutate_until_owner_closes(tmp_path: Path) -> None:
    first = SessionService.synthetic_default(tmp_path)
    second = SessionService.synthetic_default(tmp_path)
    first.handle(_command(CommandKind.PAUSE))

    with pytest.raises(SessionOwnershipError, match="active in another Heartwood process"):
        second.handle(_command(CommandKind.RESUME))

    first.close()
    result = second.handle(_command(CommandKind.RESUME))
    assert result.events[-1].kind == EventKind.SESSION_RESUMED.value


def test_writer_lease_can_be_released_from_a_gateway_worker_thread(
    tmp_path: Path,
) -> None:
    owner = FileSessionStore(tmp_path, "session-main")
    owner.acquire_writer()
    errors: list[Exception] = []

    def release() -> None:
        try:
            owner.release_writer()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=release)
    worker.start()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert not owner.owns_writer

    successor = FileSessionStore(tmp_path, "session-main")
    successor.acquire_writer()
    assert successor.owns_writer
    successor.release_writer()


def test_writer_lease_excludes_another_process(tmp_path: Path) -> None:
    owner = SessionService.synthetic_default(tmp_path)
    owner.handle(_command(CommandKind.PAUSE))
    child_script = """
import sys
from pathlib import Path
from heartwood.core_adapter import SessionOwnershipError, SessionService
from heartwood.session import CommandKind, SessionCommand

service = SessionService.synthetic_default(Path(sys.argv[1]))
command = SessionCommand(
    command_id="child-resume",
    session_id="session-synthetic-001",
    kind=CommandKind.RESUME,
    actor_id="child",
    created_at="2026-01-01T00:00:00Z",
)
try:
    service.handle(command)
except SessionOwnershipError:
    raise SystemExit(23)
service.close()
"""

    blocked = subprocess.run(
        [sys.executable, "-c", child_script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    owner.close()
    handed_off = subprocess.run(
        [sys.executable, "-c", child_script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert blocked.returncode == 23, blocked.stderr
    assert handed_off.returncode == 0, handed_off.stderr


def test_replay_waits_for_a_complete_paired_commit(tmp_path: Path) -> None:
    writer_script = """
import sys
import time
from pathlib import Path
import heartwood.core_adapter._state as state
from heartwood.core_adapter import SessionService
from heartwood.session import CommandKind, SessionCommand

root = Path(sys.argv[1])
ready = root / "audit-appended"
release = root / "release-commit"
original_append = state._append_private_json_line

def pause_after_audit(path, content):
    original_append(path, content)
    if path.name == "audit.jsonl" and not ready.exists():
        ready.write_text("ready", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)

state._append_private_json_line = pause_after_audit
service = SessionService.synthetic_default(root)
service.handle(
    SessionCommand(
        command_id="paused-pair",
        session_id="session-synthetic-001",
        kind=CommandKind.PAUSE,
        actor_id="writer",
        created_at="2026-01-01T00:00:00Z",
    )
)
service.close()
"""
    reader_script = """
import sys
from pathlib import Path
from heartwood.core_adapter import SessionService

root = Path(sys.argv[1])
(root / "reader-started").write_text("ready", encoding="utf-8")
service = SessionService.synthetic_default(root)
print(len(service.replay_events()))
service.close()
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", writer_script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    reader: subprocess.Popen[str] | None = None
    try:
        _wait_for_process_marker(writer, tmp_path / "audit-appended")
        reader = subprocess.Popen(
            [sys.executable, "-c", reader_script, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for_process_marker(reader, tmp_path / "reader-started")
        time.sleep(0.1)
        assert reader.poll() is None, reader.stderr.read() if reader.stderr is not None else ""

        (tmp_path / "release-commit").write_text("continue", encoding="utf-8")
        writer_stdout, writer_stderr = writer.communicate(timeout=5)
        reader_stdout, reader_stderr = reader.communicate(timeout=5)
    finally:
        for process in (writer, reader):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    assert writer.returncode == 0, f"{writer_stdout}\n{writer_stderr}"
    assert reader.returncode == 0, reader_stderr
    assert reader_stdout.strip() in {"1", "2"}


def test_command_waits_for_snapshot_contention_without_stranding_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder_script = """
import sys
import time
from pathlib import Path
from heartwood.core_adapter import FileSessionStore

root = Path(sys.argv[1])
store = FileSessionStore(root, "session-synthetic-001")
while not (root / "acquire-snapshot").exists():
    time.sleep(0.01)
with store.snapshot():
    (root / "snapshot-held").write_text("ready", encoding="utf-8")
    while not (root / "release-snapshot").exists():
        time.sleep(0.01)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    service = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.PAUSE, command_id="contended-pause")
    results: list[object] = []
    errors: list[Exception] = []
    original_accept = service.store.accept_command

    def accept_then_contend(
        *,
        command_id: str,
        command_hash: str,
        first_sequence: int,
    ) -> None:
        original_accept(
            command_id=command_id,
            command_hash=command_hash,
            first_sequence=first_sequence,
        )
        (tmp_path / "acquire-snapshot").write_text("ready", encoding="utf-8")
        while not (tmp_path / "snapshot-held").exists():
            time.sleep(0.01)

    monkeypatch.setattr(service.store, "accept_command", accept_then_contend)

    def handle_command() -> None:
        try:
            results.append(service.handle(command))
        except Exception as error:  # pragma: no cover - asserted through errors
            errors.append(error)

    worker = threading.Thread(target=handle_command)
    try:
        worker.start()
        _wait_for_process_marker(holder, tmp_path / "snapshot-held")
        assert service.store.command_record(command.command_id) is not None
        assert worker.is_alive()

        (tmp_path / "release-snapshot").write_text("continue", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        worker.join(timeout=5)
    finally:
        (tmp_path / "release-snapshot").touch()
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=5)
        worker.join(timeout=5)

    assert holder.returncode == 0, f"{holder_stdout}\n{holder_stderr}"
    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1
    assert service.store.unresolved_command_ids() == ()


def test_distinct_sessions_can_mutate_independently(tmp_path: Path) -> None:
    first = SessionService.synthetic_default(tmp_path, session_id="session-one")
    second = SessionService.synthetic_default(tmp_path, session_id="session-two")
    first_command = _command(CommandKind.PAUSE).model_copy(
        update={"command_id": "pause-one", "session_id": "session-one"}
    )
    second_command = _command(CommandKind.PAUSE).model_copy(
        update={"command_id": "pause-two", "session_id": "session-two"}
    )

    first_result = first.handle(first_command)
    second_result = second.handle(second_command)

    assert first_result.events[-1].kind == EventKind.SESSION_PAUSED.value
    assert second_result.events[-1].kind == EventKind.SESSION_PAUSED.value


def test_stale_writer_metadata_is_detected_and_reclaimed(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.session_dir.mkdir(mode=0o700, parents=True)
    store.writer_metadata_path.write_text(
        '{"host":"stale-host","pid":123,"token":"stale"}\n',
        encoding="utf-8",
    )
    service = SessionService.synthetic_default(tmp_path)

    service.handle(_command(CommandKind.PAUSE))

    assert service.store.recovered_stale_writer
    assert service.store.writer_metadata_path.is_file()
    service.close()
    assert not service.store.writer_metadata_path.exists()


def test_writer_lease_recovers_after_process_is_killed(tmp_path: Path) -> None:
    ready_path = tmp_path / "writer-ready"
    child_script = """
import sys
import time
from pathlib import Path
from heartwood.core_adapter import SessionService
from heartwood.session import CommandKind, SessionCommand

root = Path(sys.argv[1])
service = SessionService.synthetic_default(root)
service.handle(
    SessionCommand(
        command_id="killed-pause",
        session_id="session-synthetic-001",
        kind=CommandKind.PAUSE,
        actor_id="child",
        created_at="2026-01-01T00:00:00Z",
    )
)
(root / "writer-ready").write_text("ready", encoding="utf-8")
time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if ready_path.exists() or process.poll() is not None:
                break
            time.sleep(0.05)
        detail = (
            process.stderr.read()
            if process.poll() is not None and process.stderr is not None
            else "child writer did not become ready"
        )
        assert ready_path.is_file(), detail
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    restarted = SessionService.synthetic_default(tmp_path)
    result = restarted.handle(_command(CommandKind.RESUME, command_id="restarted-resume"))

    assert restarted.store.recovered_stale_writer
    assert result.events[-1].kind == EventKind.SESSION_RESUMED.value
    assert len(restarted.replay_events()) == 4


def test_interrupted_audit_first_commit_recovers_without_duplicate_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = session_state._append_private_json_line
    append_count = 0

    def interrupt_second_append(path: Path, content: str) -> None:
        nonlocal append_count
        append_count += 1
        if append_count == 2:
            raise OSError("simulated process interruption")
        original_append(path, content)

    monkeypatch.setattr(session_state, "_append_private_json_line", interrupt_second_append)
    service = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.PAUSE)
    with pytest.raises(OSError, match="simulated process interruption"):
        service.handle(command)
    service.store.events_path.write_bytes(b'{"partial"')
    service.close()

    restarted = SessionService.synthetic_default(tmp_path)
    replayed = restarted.replay_events()
    with pytest.raises(SessionRecoveryError, match="interrupted after acceptance"):
        restarted.handle(command)

    assert not restarted.store.pending_commit_path.exists()
    assert len(restarted.audit_log.read()) == 1
    assert len(replayed) == 1
    assert replayed[0].kind == EventKind.COMMAND_RECEIVED.value


def test_tampered_pending_commit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_append(_path: Path, _content: str) -> None:
        raise OSError("simulated process interruption")

    monkeypatch.setattr(session_state, "_append_private_json_line", interrupt_append)
    service = SessionService.synthetic_default(tmp_path)
    with pytest.raises(OSError, match="simulated process interruption"):
        service.handle(_command(CommandKind.PAUSE))
    pending = json.loads(service.store.pending_commit_path.read_text(encoding="utf-8"))
    pending["session_event"]["payload"]["command_id"] = "tampered"
    service.store.pending_commit_path.write_text(json.dumps(pending), encoding="utf-8")
    service.close()
    recovered = FileSessionStore(tmp_path, "session-synthetic-001")
    recovered.acquire_writer()

    with pytest.raises(SessionRecoveryError, match="do not correspond"):
        recovered.recover_pending_commit()


def test_recovery_does_not_mutate_corrupt_committed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = session_state._append_private_json_line
    append_count = 0

    def interrupt_second_append(path: Path, content: str) -> None:
        nonlocal append_count
        append_count += 1
        if append_count == 2:
            raise OSError("simulated process interruption")
        original_append(path, content)

    monkeypatch.setattr(session_state, "_append_private_json_line", interrupt_second_append)
    service = SessionService.synthetic_default(tmp_path)
    with pytest.raises(OSError, match="simulated process interruption"):
        service.handle(_command(CommandKind.PAUSE))
    service.store.events_path.write_bytes(b'{"partial"')
    audit_record = json.loads(service.store.audit_path.read_text(encoding="utf-8"))
    audit_record["event_type"] = EventKind.SESSION_RESUMED.value
    service.store.audit_path.write_text(json.dumps(audit_record) + "\n", encoding="utf-8")
    audit_before = service.store.audit_path.read_bytes()
    events_before = service.store.events_path.read_bytes()
    service.close()
    recovered = FileSessionStore(tmp_path, "session-synthetic-001")
    recovered.acquire_writer()

    with pytest.raises(SessionRecoveryError, match="existing audit prefix is invalid"):
        recovered.recover_pending_commit()

    assert recovered.audit_path.read_bytes() == audit_before
    assert recovered.events_path.read_bytes() == events_before


def test_command_remains_uncertain_if_completion_receipt_write_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = session_state._write_private_json_atomic

    def interrupt_completion(path: Path, payload: dict[str, object]) -> None:
        if path.parent.name == ".commands" and payload.get("state") == "completed":
            raise OSError("simulated receipt interruption")
        original_write(path, payload)

    monkeypatch.setattr(session_state, "_write_private_json_atomic", interrupt_completion)
    service = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.PAUSE)
    with pytest.raises(OSError, match="simulated receipt interruption"):
        service.handle(command)
    monkeypatch.setattr(session_state, "_write_private_json_atomic", original_write)

    with pytest.raises(SessionRecoveryError, match="will not be executed again automatically"):
        service.handle(command)

    assert [event.kind for event in service.replay_events()] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.SESSION_PAUSED.value,
    ]


def test_interrupted_approval_receipt_recovers_without_repeating_tool_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = ProposedToolCall(
        tool_call_id="session-main-action",
        tool_name="file_editor",
        risk="medium",
        summary="write one file",
    )
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(
            BackendToolCallEvent(tool_call=tool_call),
            BackendConfirmationRequestEvent(
                tool_call=tool_call,
                action_group_id=_action_group_id(tool_call.tool_call_id),
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    service.handle(
        _command(CommandKind.CHAT, prompt="write").model_copy(update={"session_id": "session-main"})
    )
    original_write = session_state._write_private_json_atomic

    def interrupt_approval_completion(path: Path, payload: dict[str, object]) -> None:
        if (
            path.parent.name == ".commands"
            and payload.get("command_id") == "command-approve"
            and payload.get("state") == "completed"
        ):
            raise OSError("simulated approval receipt interruption")
        original_write(path, payload)

    monkeypatch.setattr(
        session_state,
        "_write_private_json_atomic",
        interrupt_approval_completion,
    )
    approval = _command(
        CommandKind.APPROVE,
        target_type="action-set",
        target_id=_action_group_id(tool_call.tool_call_id),
    ).model_copy(update={"session_id": "session-main"})
    with pytest.raises(OSError, match="simulated approval receipt interruption"):
        service.handle(approval)
    monkeypatch.setattr(session_state, "_write_private_json_atomic", original_write)

    replayed = service.handle(approval)

    assert replayed.replayed
    assert backend.resolutions == [(_action_group_id(tool_call.tool_call_id), True)]


def test_approval_intent_recovers_when_interrupted_before_backend_transition(
    tmp_path: Path,
) -> None:
    tool_call = ProposedToolCall(
        tool_call_id="session-main-action",
        tool_name="file_editor",
        risk="medium",
        summary="write one file",
    )
    backend = _InterruptBeforeResolutionBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(
            BackendToolCallEvent(tool_call=tool_call),
            BackendConfirmationRequestEvent(
                tool_call=tool_call,
                action_group_id=_action_group_id(tool_call.tool_call_id),
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    service.handle(
        _command(CommandKind.CHAT, prompt="write").model_copy(update={"session_id": "session-main"})
    )
    approval = _command(
        CommandKind.APPROVE,
        target_type="action-set",
        target_id=_action_group_id(tool_call.tool_call_id),
    ).model_copy(update={"session_id": "session-main"})

    with pytest.raises(OSError, match="before backend transition"):
        service.handle(approval)
    recovered = service.reconcile()
    denied = service.handle(
        _command(
            CommandKind.DENY,
            target_type="action-set",
            target_id=_action_group_id(tool_call.tool_call_id),
        ).model_copy(update={"session_id": "session-main"})
    )
    replayed = service.handle(approval)

    assert replayed.replayed
    assert backend.resolutions == [(_action_group_id(tool_call.tool_call_id), True)]
    assert any(event.kind == EventKind.CONFIRMATION_RESOLVED.value for event in recovered)
    assert denied.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert denied.events[-1].payload["reason"] == (
        f"no matching pending action group: {_action_group_id(tool_call.tool_call_id)}"
    )


def test_approval_intent_recovers_from_backend_state_after_interrupted_return(
    tmp_path: Path,
) -> None:
    tool_call = ProposedToolCall(
        tool_call_id="session-main-action",
        tool_name="file_editor",
        risk="medium",
        summary="write one file",
    )
    backend = _InterruptAfterResolutionBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(
            BackendToolCallEvent(tool_call=tool_call),
            BackendConfirmationRequestEvent(
                tool_call=tool_call,
                action_group_id=_action_group_id(tool_call.tool_call_id),
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    service.handle(
        _command(CommandKind.CHAT, prompt="write").model_copy(update={"session_id": "session-main"})
    )
    approval = _command(
        CommandKind.APPROVE,
        target_type="action-set",
        target_id=_action_group_id(tool_call.tool_call_id),
    ).model_copy(update={"session_id": "session-main"})

    with pytest.raises(OSError, match="after backend transition"):
        service.handle(approval)
    replayed = service.handle(approval)

    assert replayed.replayed
    assert backend.resolutions == [(_action_group_id(tool_call.tool_call_id), True)]


def test_session_control_files_are_private(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.PAUSE))
    receipt_paths = tuple(service.store.commands_dir.iterdir())

    assert stat.S_IMODE(service.store.writer_lock_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(service.store.writer_metadata_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(service.store.commands_dir.stat().st_mode) == 0o700
    assert len(receipt_paths) == 1
    assert stat.S_IMODE(receipt_paths[0].stat().st_mode) == 0o600


def test_command_receipts_cannot_follow_symbolic_link(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.session_dir.mkdir(mode=0o700, parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    store.commands_dir.symlink_to(outside, target_is_directory=True)
    service = SessionService.synthetic_default(tmp_path)

    with pytest.raises(SessionStoreBoundaryError, match="receipt path"):
        service.handle(_command(CommandKind.PAUSE))

    assert tuple(outside.iterdir()) == ()


def test_replay_rejects_tampered_session_event_payload(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.PAUSE))
    lines = service.store.events_path.read_text(encoding="utf-8").splitlines()
    changed = json.loads(lines[1])
    changed["payload"]["command_id"] = "tampered-command"
    lines[1] = json.dumps(changed)
    service.store.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="session event hash mismatch"):
        service.replay_events()


def test_replay_rejects_truncated_session_event_stream(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.PAUSE))
    first = service.store.events_path.read_text(encoding="utf-8").splitlines()[0]
    service.store.events_path.write_text(first + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="different lengths"):
        service.replay_events()


def test_replay_rejects_interrupted_audit_first_append(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.PAUSE))
    service.audit_log.append(
        session_id=service.store.session_id,
        event_type=EventKind.SESSION_RESUMED.value,
        occurred_at="2026-01-01T00:00:00Z",
        payload={"session_event_hash": "sha256:" + "0" * 64},
    )

    with pytest.raises(AuditIntegrityError, match="different lengths"):
        service.replay_events()


def test_file_store_rejects_session_ids_outside_the_workspace(tmp_path: Path) -> None:
    with pytest.raises(SessionStoreBoundaryError, match="session id must start"):
        FileSessionStore(tmp_path, "../escape")


def test_file_store_rejects_symbolic_link_session_alias(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    target = sessions / "target"
    target.mkdir(parents=True)
    (sessions / "linked-session").symlink_to(target, target_is_directory=True)

    with pytest.raises(SessionStoreBoundaryError, match="symbolic link"):
        FileSessionStore(sessions, "linked-session")


def test_session_store_mutations_require_writer_ownership(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    event, audit_event = _event_pair()
    store.session_dir.mkdir(mode=0o700, parents=True)
    store.pending_commit_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SessionOwnershipError):
        store.commit_event(event, audit_event)
    with pytest.raises(SessionRecoveryError, match="writer-owned"):
        store.recover_pending_commit()
    with pytest.raises(SessionOwnershipError):
        store.accept_command(command_id="command", command_hash="sha256:value", first_sequence=0)
    with pytest.raises(SessionOwnershipError):
        store.complete_command(
            command_id="command",
            command_hash="sha256:value",
            first_sequence=0,
            last_sequence=0,
        )
    with pytest.raises(SessionOwnershipError):
        store.record_completed_legacy_command(
            command_id="command",
            command_hash="sha256:value",
            first_sequence=0,
            last_sequence=0,
        )
    with pytest.raises(SessionOwnershipError):
        store.write_audit_export("synthetic\n")
    store.release_writer()


def test_session_store_rejects_mismatched_commit_identity_and_sequence(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    event, audit_event = _event_pair()

    with pytest.raises(SessionRecoveryError, match="session does not match"):
        store.commit_event(
            event,
            audit_event.model_copy(update={"session_id": "session-other"}),
        )
    with pytest.raises(SessionRecoveryError, match="sequences do not match"):
        store.commit_event(
            event,
            audit_event.model_copy(update={"sequence": 1}),
        )


def test_session_store_rejects_symbolic_link_writer_lock(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.session_dir.mkdir(mode=0o700, parents=True)
    target = tmp_path / "outside-lock"
    target.touch()
    store.writer_lock_path.symlink_to(target)

    with pytest.raises(SessionStoreBoundaryError, match="writer lock"):
        store.acquire_writer()


def test_writer_setup_failure_releases_operating_system_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = session_state._write_private_json_atomic

    def fail_metadata(path: Path, payload: dict[str, object]) -> None:
        if path.name == ".writer.json":
            raise OSError("metadata write failed")
        original_write(path, payload)

    monkeypatch.setattr(session_state, "_write_private_json_atomic", fail_metadata)
    first = FileSessionStore(tmp_path, "session-synthetic-001")
    with pytest.raises(OSError, match="metadata write failed"):
        first.acquire_writer()
    monkeypatch.setattr(session_state, "_write_private_json_atomic", original_write)
    second = FileSessionStore(tmp_path, "session-synthetic-001")

    second.acquire_writer()

    assert second.owns_writer


def test_session_locks_fail_closed_when_native_locking_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_lock_unavailable(_descriptor: int) -> bool:
        raise OSError(errno.ENOSYS, "synthetic unsupported filesystem")

    monkeypatch.setattr("filelock._unix._lock_fd_nonblocking", native_lock_unavailable)

    writer_store = FileSessionStore(tmp_path / "writer", "session-synthetic-001")
    with pytest.raises(SessionStorageCapabilityError, match="required process locks"):
        writer_store.acquire_writer()
    assert not writer_store.owns_writer

    snapshot_store = FileSessionStore(tmp_path / "snapshot", "session-synthetic-001")
    with (
        pytest.raises(SessionStorageCapabilityError, match="required process locks"),
        snapshot_store.snapshot(),
    ):
        pytest.fail("snapshot unexpectedly acquired a soft lock")


def test_command_receipt_state_machine_rejects_invalid_transitions(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    store.accept_command(
        command_id="command-one",
        command_hash="sha256:one",
        first_sequence=0,
    )

    assert store.unresolved_command_ids() == ("command-one",)
    with pytest.raises(SessionRecoveryError, match="already exists"):
        store.accept_command(
            command_id="command-one",
            command_hash="sha256:one",
            first_sequence=0,
        )
    with pytest.raises(SessionRecoveryError, match="sequence is invalid"):
        store.complete_command(
            command_id="command-one",
            command_hash="sha256:one",
            first_sequence=1,
            last_sequence=0,
        )
    with pytest.raises(SessionRecoveryError, match="receipt changed"):
        store.complete_command(
            command_id="command-one",
            command_hash="sha256:different",
            first_sequence=0,
            last_sequence=0,
        )

    store.complete_command(
        command_id="command-one",
        command_hash="sha256:one",
        first_sequence=0,
        last_sequence=0,
    )
    store.record_completed_legacy_command(
        command_id="command-one",
        command_hash="sha256:one",
        first_sequence=0,
        last_sequence=0,
    )
    assert store.unresolved_command_ids() == ()


def test_pending_commit_recovery_rejects_malformed_and_mismatched_state(
    tmp_path: Path,
) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    store.pending_commit_path.write_text(
        '{"schema_version":"unsupported"}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionRecoveryError, match="unsupported"):
        store.recover_pending_commit()

    store.pending_commit_path.write_text("{", encoding="utf-8")
    with pytest.raises(SessionRecoveryError, match="malformed"):
        store.recover_pending_commit()

    other_event, other_audit = _event_pair(session_id="session-other")
    _write_pending_commit(store, other_event, other_audit)
    with pytest.raises(SessionRecoveryError, match="identity does not match"):
        store.recover_pending_commit()

    event, audit_event = _event_pair()
    _write_pending_commit(
        store,
        event,
        audit_event.model_copy(update={"event_hash": "sha256:" + "0" * 64}),
    )
    with pytest.raises(SessionRecoveryError, match="audit event hash"):
        store.recover_pending_commit()


def test_pending_commit_recovery_rejects_invalid_chain_and_position(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    first_event, first_audit = _event_pair()
    store.commit_event(first_event, first_audit)

    wrong_position_event, wrong_position_audit = _event_pair(kind=EventKind.SESSION_RESUMED)
    _write_pending_commit(store, wrong_position_event, wrong_position_audit)
    with pytest.raises(SessionRecoveryError, match="does not match the pending commit"):
        store.recover_pending_commit()

    bad_previous = "sha256:" + "f" * 64
    next_event, next_audit = _event_pair(sequence=1, previous_event_hash=bad_previous)
    _write_pending_commit(store, next_event, next_audit)
    with pytest.raises(SessionRecoveryError, match="previous hash does not match"):
        store.recover_pending_commit()


def test_pending_first_commit_rejects_previous_hash(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    event, audit_event = _event_pair(previous_event_hash="sha256:" + "f" * 64)
    _write_pending_commit(store, event, audit_event)

    with pytest.raises(SessionRecoveryError, match="first pending commit"):
        store.recover_pending_commit()


def test_pending_second_commit_recovers_with_valid_previous_hash(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    first_event, first_audit = _event_pair()
    store.commit_event(first_event, first_audit)
    second_event, second_audit = _event_pair(
        sequence=1,
        kind=EventKind.SESSION_RESUMED,
        previous_event_hash=first_audit.event_hash,
    )
    _write_pending_commit(store, second_event, second_audit)

    assert store.recover_pending_commit()
    assert [event.kind for event in store.read_events()] == [
        EventKind.SESSION_PAUSED.value,
        EventKind.SESSION_RESUMED.value,
    ]
    assert not store.recover_pending_commit()


def test_pending_commit_recovers_partial_audit_tail(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    event, audit_event = _event_pair()
    _write_pending_commit(store, event, audit_event)
    store.audit_path.write_bytes(b'{"partial"')

    assert store.recover_pending_commit()
    assert store.audit_path.read_text(encoding="utf-8").endswith("\n")
    assert len(store.read_events()) == 1


def test_command_record_validation_rejects_malformed_receipts(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path, "session-synthetic-001")
    store.acquire_writer()
    store.commands_dir.mkdir(mode=0o700)
    path = store._command_path("command")

    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(SessionRecoveryError, match="malformed"):
        store.command_record("command")

    invalid_receipts = (
        {
            "schema_version": "heartwood.session-command-receipt.v1",
            "session_id": store.session_id,
            "command_id": "command",
            "command_hash": "sha256:value",
            "state": "accepted",
            "first_sequence": -1,
            "last_sequence": None,
        },
        {
            "schema_version": "heartwood.session-command-receipt.v1",
            "session_id": store.session_id,
            "command_id": "command",
            "command_hash": "sha256:value",
            "state": "completed",
            "first_sequence": 0,
            "last_sequence": None,
        },
        {
            "schema_version": "heartwood.session-command-receipt.v1",
            "session_id": store.session_id,
            "command_id": "command",
            "command_hash": "sha256:value",
            "state": "accepted",
            "first_sequence": 0,
            "last_sequence": 0,
        },
    )
    for receipt in invalid_receipts:
        path.write_text(json.dumps(receipt), encoding="utf-8")
        with pytest.raises(SessionRecoveryError, match="receipt"):
            store.command_record("command")

    other_session = {
        **invalid_receipts[0],
        "session_id": "session-other",
        "first_sequence": 0,
    }
    path.write_text(json.dumps(other_session), encoding="utf-8")
    with pytest.raises(SessionRecoveryError, match="another session"):
        store.command_record("command")


def test_unresolved_receipt_scan_rejects_invalid_storage(tmp_path: Path) -> None:
    symlink_store = FileSessionStore(tmp_path / "symlink", "session-synthetic-001")
    symlink_store.session_dir.mkdir(mode=0o700, parents=True)
    outside = tmp_path / "outside-receipts"
    outside.mkdir()
    symlink_store.commands_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SessionStoreBoundaryError, match="symbolic link"):
        symlink_store.unresolved_command_ids()

    file_store = FileSessionStore(tmp_path / "file", "session-synthetic-001")
    file_store.session_dir.mkdir(mode=0o700, parents=True)
    file_store.commands_dir.write_text("not a directory", encoding="utf-8")
    with pytest.raises(SessionStoreBoundaryError, match="must be a directory"):
        file_store.unresolved_command_ids()

    malformed_store = FileSessionStore(tmp_path / "malformed", "session-synthetic-001")
    malformed_store.commands_dir.mkdir(mode=0o700, parents=True)
    (malformed_store.commands_dir / "receipt.json").write_text("[]", encoding="utf-8")
    with pytest.raises(SessionRecoveryError, match="malformed"):
        malformed_store.unresolved_command_ids()
    receipt_path = malformed_store.commands_dir / "receipt.json"
    receipt_path.write_text('{"command_id":7}', encoding="utf-8")
    with pytest.raises(SessionRecoveryError, match="invalid"):
        malformed_store.unresolved_command_ids()
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.session-command-receipt.v1",
                "session_id": "session-other",
                "command_id": "command",
                "command_hash": "sha256:value",
                "state": "accepted",
                "first_sequence": 0,
                "last_sequence": None,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SessionRecoveryError, match="another session"):
        malformed_store.unresolved_command_ids()


def test_legacy_command_events_gain_completed_receipt_on_retry(tmp_path: Path) -> None:
    first = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.PAUSE)
    original = first.handle(command)
    first.close()
    for receipt in first.store.commands_dir.iterdir():
        receipt.unlink()
    first.store.commands_dir.rmdir()
    restarted = SessionService.synthetic_default(tmp_path)

    replayed = restarted.handle(command)

    assert replayed.events == original.events
    assert replayed.replayed
    assert restarted.store.command_record(command.command_id) is not None


def test_completed_receipt_must_match_persisted_event_range(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    command = _command(CommandKind.PAUSE)
    service.handle(command)
    receipt_path = service.store._command_path(command.command_id)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["last_sequence"] = 10
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SessionRecoveryError, match="events do not match"):
        service.handle(command)


def test_task_records_route_decision_and_waits_for_action_confirmation(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.CHAT, prompt="summarize the cohort"))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.AGENT_MESSAGE_EMITTED.value,
        EventKind.TOOL_CALL_PROPOSED.value,
        EventKind.CONFIRMATION_REQUESTED.value,
    ]
    assert result.events[1].payload == {
        "actor_id": "human",
        "command_id": "command-chat",
        "content": "summarize the cohort",
    }
    decision = result.events[2].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "allow"
    profile = result.events[2].payload["model_profile"]
    assert isinstance(profile, dict)
    assert profile["profile_id"] == "deterministic-local"
    assert "summarize the cohort" in json.dumps(
        [event.model_dump(mode="json") for event in service.replay_events()]
    )
    audit_text = service.store.audit_path.read_text(encoding="utf-8")
    assert "summarize the cohort" not in audit_text
    assert '"content_chars":20' in audit_text
    assert stat.S_IMODE(service.store.session_dir.stat().st_mode) == 0o700
    for path in (
        service.store.events_path,
        service.store.audit_path,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_risk_based_mode_auto_executes_low_risk_action_and_records_mode(
    tmp_path: Path,
) -> None:
    service = SessionService.local_default(
        tmp_path,
        session_id="session-synthetic-001",
        backend=DeterministicAgentBackend(action_confirmation_mode="confirm-risky"),
        env={},
    )

    result = service.handle(_command(CommandKind.CHAT, prompt="summarize the cohort"))

    kinds = [event.kind for event in result.events]
    assert EventKind.TOOL_CALL_PROPOSED.value in kinds
    assert EventKind.TOOL_EXECUTION_RECORDED.value in kinds
    assert EventKind.CONFIRMATION_REQUESTED.value not in kinds
    profile = result.events[2].payload["model_profile"]
    assert isinstance(profile, dict)
    assert profile["action_confirmation_mode"] == "confirm-risky"


def test_approved_action_records_tool_execution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="run the workflow"))

    result = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="action-set",
            target_id=_action_group_id("session-synthetic-001-toolcall-0"),
        )
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.APPROVAL_RECORDED.value,
        EventKind.CONFIRMATION_RESOLVED.value,
        EventKind.TOOL_EXECUTION_RECORDED.value,
    ]
    assert result.events[1].payload["decision"] == "approved"
    assert result.events[3].payload["exit_code"] == 0
    assert result.events[3].payload["tool_call_id"] == "session-synthetic-001-toolcall-0"
    assert result.events[3].payload["action_id"] is None
    assert _audit_payload(
        EventKind.TOOL_EXECUTION_RECORDED,
        result.events[3].payload,
    ) == {
        "backend_id": "deterministic-local",
        "tool_call_id": "session-synthetic-001-toolcall-0",
        "action_id": None,
        "tool_name": "heartwood.synthetic.noop",
        "exit_code": 0,
    }


def test_rejected_action_is_not_recorded_as_tool_execution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="run the workflow"))

    result = service.handle(
        _command(
            CommandKind.DENY,
            target_type="action-set",
            target_id=_action_group_id("session-synthetic-001-toolcall-0"),
        )
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.APPROVAL_RECORDED.value,
        EventKind.CONFIRMATION_RESOLVED.value,
    ]
    assert result.events[1].payload["decision"] == "denied"


def test_failed_backend_decision_keeps_the_action_group_pending(
    tmp_path: Path,
) -> None:
    action = ProposedToolCall(
        tool_call_id="tool-1",
        tool_name="terminal",
        risk="medium",
        summary="Run the synthetic command",
    )
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        pending_actions=(action,),
        resolution_response=(
            BackendErrorEvent(
                error_code=BackendErrorCode.WORKER_STOPPED,
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="action-set",
            target_id=_action_group_id(action.tool_call_id),
        ).model_copy(update={"session_id": "session-main"})
    )

    assert EventKind.APPROVAL_RECORDED.value in [event.kind for event in result.events]
    assert EventKind.CONFIRMATION_RESOLVED.value not in [event.kind for event in result.events]
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert backend.pending_action_group(session_id="session-main") is not None


def test_interactive_approval_rejects_non_action_targets(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(
        _command(CommandKind.APPROVE, target_type="model-call", target_id="route")
    )

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert "complete pending action set" in str(result.events[-1].payload["reason"])


def test_action_decision_requires_matching_pending_action(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(
        _command(CommandKind.APPROVE, target_type="action-set", target_id="missing")
    )

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert "no matching pending action" in str(result.events[-1].payload["reason"])


def test_second_task_requires_pending_action_resolution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="first task"))

    result = service.handle(
        _command(CommandKind.CHAT, command_id="command-chat-2", prompt="second task")
    )

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert "resolve the pending action" in str(result.events[-1].payload["reason"])


def test_resume_requires_pending_action_resolution(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)
    service.handle(_command(CommandKind.CHAT, prompt="propose an action"))

    result = service.handle(_command(CommandKind.RESUME))

    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].payload["reason"] == "resolve the pending action before resuming"


def test_denied_route_never_calls_backend(tmp_path: Path) -> None:
    backend = _RecordingBackend(endpoint="https://public.example.invalid/v1/chat/completions")
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        policy_profile=PolicyProfile(
            policy_id="local-only",
            platform_id="generic",
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            credential_allowlist=(),
        ),
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.CHAT, prompt="do not send").model_copy(
            update={"session_id": "session-main"}
        )
    )

    assert backend.prompts == []
    decision = result.events[2].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"
    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value


def test_approved_action_rechecks_route_before_backend_continuation(tmp_path: Path) -> None:
    pending = ProposedToolCall(
        tool_call_id="session-main-action",
        tool_name="terminal",
        risk="low",
        summary="run a bounded command",
    )
    backend = _RecordingBackend(
        endpoint="http://127.0.0.1:8765/v1/chat/completions",
        response=(
            BackendToolCallEvent(tool_call=pending),
            BackendConfirmationRequestEvent(
                tool_call=pending,
                action_group_id=_action_group_id(pending.tool_call_id),
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        policy_profile=PolicyProfile(
            policy_id="local-only",
            platform_id="generic",
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            credential_allowlist=(),
        ),
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    command = _command(CommandKind.CHAT, prompt="propose action").model_copy(
        update={"session_id": "session-main"}
    )
    service.handle(command)
    backend.endpoint = "https://public.example.invalid/v1/chat/completions"

    result = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="action-set",
            target_id=_action_group_id(pending.tool_call_id),
        ).model_copy(update={"session_id": "session-main"})
    )

    assert backend.resolutions == []
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    decision = result.events[1].payload["decision"]
    assert isinstance(decision, dict)
    assert decision["decision"] == "deny"


def test_service_uses_backend_owned_pending_group_after_restart(tmp_path: Path) -> None:
    first = ProposedToolCall(
        tool_call_id="session-main-action-1",
        tool_name="terminal",
        risk="medium",
        summary="run the first bounded command",
        arguments={"command": "python first.py"},
    )
    second = ProposedToolCall(
        tool_call_id="session-main-action-2",
        tool_name="terminal",
        risk="unknown",
        summary="run the second bounded command",
    )
    initial_backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(
            BackendToolCallEvent(tool_call=first),
            BackendToolCallEvent(tool_call=second),
            BackendConfirmationRequestEvent(
                tool_call=first,
                action_group_id=_action_group_id(first.tool_call_id, second.tool_call_id),
            ),
            BackendConfirmationRequestEvent(
                tool_call=second,
                action_group_id=_action_group_id(first.tool_call_id, second.tool_call_id),
            ),
        ),
    )
    initial_service = SessionService.local_default(
        tmp_path,
        backend=initial_backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    initial_service.handle(
        _command(CommandKind.CHAT, prompt="propose two actions").model_copy(
            update={"session_id": "session-main"}
        )
    )
    initial_service.close()

    restored_backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        pending_actions=(first, second),
    )
    restored_service = SessionService.local_default(
        tmp_path,
        backend=restored_backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = restored_service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="action-set",
            target_id=_action_group_id(first.tool_call_id, second.tool_call_id),
        ).model_copy(update={"session_id": "session-main"})
    )

    assert restored_backend.resolutions == [
        (_action_group_id(first.tool_call_id, second.tool_call_id), True)
    ]
    assert any(
        event.kind == EventKind.MODEL_CALL_DECISION_RECORDED.value for event in result.events
    )


def test_corrupt_deterministic_pending_state_blocks_restart_without_changing_history(
    tmp_path: Path,
) -> None:
    service = SessionService.local_default(
        tmp_path,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    result = service.handle(
        _command(CommandKind.CHAT, prompt="propose one action").model_copy(
            update={"session_id": "session-main"}
        )
    )
    service.close()
    event_log = service.store.events_path
    audit_log = service.audit_log.path
    state_path = service.store.session_dir / ".deterministic-backend.json"
    events_before = event_log.read_bytes()
    audit_before = audit_log.read_bytes()
    state_path.write_text("{interrupted", encoding="utf-8")

    with pytest.raises(ValueError, match="deterministic backend state is invalid"):
        SessionService.local_default(
            tmp_path,
            clock=lambda: "2026-01-01T00:00:00Z",
        )

    assert any(event.kind == EventKind.CONFIRMATION_REQUESTED.value for event in result.events)
    assert event_log.read_bytes() == events_before
    assert audit_log.read_bytes() == audit_before
    assert state_path.read_text(encoding="utf-8") == "{interrupted"


def test_resume_rechecks_route_before_backend_continuation(tmp_path: Path) -> None:
    backend = _RecordingBackend(endpoint="https://public.example.invalid/v1/chat/completions")
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        policy_profile=PolicyProfile(
            policy_id="local-only",
            platform_id="generic",
            deny_egress_by_default=True,
            allowed_model_endpoints=("http://127.0.0.1:8765/v1/chat/completions",),
            credential_allowlist=(),
        ),
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.RESUME).model_copy(update={"session_id": "session-main"})
    )

    assert backend.resume_calls == 0
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.MODEL_CALL_DECISION_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]


def test_backend_configuration_fails_before_route_decision(tmp_path: Path) -> None:
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        configuration_error="model profile is not ready",
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.CHAT, prompt="do not send").model_copy(
            update={"session_id": "session-main"}
        )
    )

    assert backend.prompts == []
    assert [event.kind for event in result.events] == [
        EventKind.COMMAND_RECEIVED.value,
        EventKind.USER_MESSAGE_RECORDED.value,
        EventKind.ERROR_RECORDED.value,
    ]
    assert result.events[-1].payload["reason"] == "model profile is not ready"


def test_backend_error_is_translated_without_exception(tmp_path: Path) -> None:
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(BackendErrorEvent(error_code=BackendErrorCode.UNKNOWN),),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(
        _command(CommandKind.CHAT, prompt="run").model_copy(update={"session_id": "session-main"})
    )

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert result.events[-1].payload["code"] == "HW-AGENT-999"
    assert result.events[-1].payload["reason"] == "The agent runtime reported an error"
    audit_text = service.store.audit_path.read_text(encoding="utf-8")
    assert '"reason":"[scrubbed]"' in audit_text


def test_reconciliation_reuses_source_event_index_across_appends_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_backend_event = BackendAgentMessageEvent(
        message="first",
        source_event_id="source-first",
    )
    second_backend_event = BackendAgentMessageEvent(
        message="second",
        source_event_id="source-second",
    )
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        reconciled=(first_backend_event,),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    original_read_events = service.store.read_events
    read_count = 0

    def count_read_events() -> tuple[SessionEvent, ...]:
        nonlocal read_count
        read_count += 1
        return original_read_events()

    monkeypatch.setattr(service.store, "read_events", count_read_events)

    assert len(service.reconcile()) == 1
    reads_after_hydration = read_count
    assert reads_after_hydration > 0
    backend._reconciled = (first_backend_event, second_backend_event)
    assert len(service.reconcile()) == 1
    assert service.reconcile() == ()
    assert read_count == reads_after_hydration
    service.close()

    restarted_backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        reconciled=(first_backend_event, second_backend_event),
    )
    restarted = SessionService.local_default(
        tmp_path,
        backend=restarted_backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    restarted_original_read_events = restarted.store.read_events
    restarted_read_count = 0

    def count_restarted_read_events() -> tuple[SessionEvent, ...]:
        nonlocal restarted_read_count
        restarted_read_count += 1
        return restarted_original_read_events()

    monkeypatch.setattr(restarted.store, "read_events", count_restarted_read_events)

    assert restarted.reconcile() == ()
    reads_after_restart_hydration = restarted_read_count
    assert reads_after_restart_hydration > 0
    assert restarted.reconcile() == ()
    assert restarted_read_count == reads_after_restart_hydration
    restarted.close()


def test_reconciliation_rebuilds_source_event_index_after_commit_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_backend_event = BackendAgentMessageEvent(
        message="first",
        source_event_id="source-first",
    )
    recovered_backend_event = BackendAgentMessageEvent(
        message="recovered",
        source_event_id="source-recovered",
    )
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        reconciled=(first_backend_event,),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    assert len(service.reconcile()) == 1
    backend._reconciled = (first_backend_event, recovered_backend_event)
    original_append = session_state._append_private_json_line
    append_count = 0

    def interrupt_event_append(path: Path, content: str) -> None:
        nonlocal append_count
        append_count += 1
        if append_count == 2:
            raise OSError("simulated event append interruption")
        original_append(path, content)

    monkeypatch.setattr(
        session_state,
        "_append_private_json_line",
        interrupt_event_append,
    )
    with pytest.raises(OSError, match="simulated event append interruption"):
        service.reconcile()
    monkeypatch.setattr(session_state, "_append_private_json_line", original_append)

    assert service.reconcile() == ()
    source_event_ids = [event.payload.get("source_event_id") for event in service.replay_events()]
    assert source_event_ids == ["source-first", "source-recovered"]


def test_task_titles_stay_out_of_audit_while_usage_remains_verifiable(
    tmp_path: Path,
) -> None:
    private_task_title = "Compare the named participant cohort"
    backend = _RecordingBackend(
        endpoint="https://model.local.invalid/v1/chat/completions",
        response=(
            BackendTaskPlanEvent(
                tasks=(
                    BackendTask(
                        title=private_task_title,
                        status=BackendTaskStatus.IN_PROGRESS,
                    ),
                ),
            ),
            BackendUsageEvent(
                usage=BackendUsage(
                    usage_id="agent",
                    model_name="synthetic-model",
                    call_count=2,
                    prompt_tokens=120,
                    completion_tokens=30,
                ),
            ),
        ),
    )
    service = SessionService.local_default(
        tmp_path,
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    service.handle(
        _command(CommandKind.CHAT, prompt="analyze").model_copy(
            update={"session_id": "session-main"}
        )
    )

    replay_text = json.dumps([event.model_dump(mode="json") for event in service.replay_events()])
    audit_text = service.store.audit_path.read_text(encoding="utf-8")
    assert private_task_title in replay_text
    assert private_task_title not in audit_text
    assert '"task_count":1' in audit_text
    assert '"in-progress":1' in audit_text
    assert '"usage_id":"agent"' in audit_text
    assert '"call_count":2' in audit_text


def test_empty_prompt_is_rejected_before_backend(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    result = service.handle(_command(CommandKind.CHAT, prompt="  "))

    assert result.events[-1].kind == EventKind.ERROR_RECORDED.value
    assert result.events[-1].payload["reason"] == "prompt is required"


def test_pause_resume_and_export_are_persisted(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    paused = service.handle(_command(CommandKind.PAUSE))
    resumed = service.handle(_command(CommandKind.RESUME))
    exported = service.handle(_command(CommandKind.AUDIT_EXPORT))

    assert paused.events[-1].kind == EventKind.SESSION_PAUSED.value
    assert resumed.events[-1].kind == EventKind.SESSION_RESUMED.value
    assert exported.events[-1].kind == EventKind.AUDIT_EXPORT_RECORDED.value
    path = Path(str(exported.events[-1].payload["path"]))
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "audit.export.recorded" in path.read_text(encoding="utf-8")
    assert service.store.read_audit_export() == path.read_text(encoding="utf-8")


def test_service_rejects_command_for_another_session(tmp_path: Path) -> None:
    service = SessionService.synthetic_default(tmp_path)

    with pytest.raises(ValueError, match="does not match"):
        service.handle(_command(CommandKind.PAUSE).model_copy(update={"session_id": "other"}))


def test_local_workspace_backend_writes_only_after_allow_once(tmp_path: Path) -> None:
    service = SessionService.local_default(
        tmp_path,
        session_id="session-local",
        env={},
        backend=LocalWorkspaceAgentBackend(tmp_path / "session-local" / "agent-artifacts"),
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    command = _command(CommandKind.CHAT, prompt="write summary").model_copy(
        update={"session_id": "session-local"}
    )
    service.handle(command)
    artifact = tmp_path / "session-local" / "agent-artifacts" / "synthetic-workspace-summary.md"
    assert not artifact.exists()

    approved = service.handle(
        _command(
            CommandKind.APPROVE,
            target_type="action-set",
            target_id=_action_group_id("session-local-toolcall-0"),
        ).model_copy(update={"session_id": "session-local"})
    )

    assert artifact.is_file()
    assert [event.kind for event in approved.events][-2:] == [
        EventKind.CONFIRMATION_RESOLVED.value,
        EventKind.TOOL_EXECUTION_RECORDED.value,
    ]
    assert "Persisted prompt content: none" in artifact.read_text(encoding="utf-8")


class _RecordingBackend:
    def __init__(
        self,
        *,
        endpoint: str,
        response: tuple[BackendEvent, ...] = (),
        configuration_error: str | None = None,
        pending_actions: tuple[ProposedToolCall, ...] = (),
        reconciled: tuple[BackendEvent, ...] = (),
        resolution_response: tuple[BackendEvent, ...] = (),
    ) -> None:
        self.endpoint = endpoint
        self.response = response
        self._configuration_error = configuration_error
        self._pending_actions = pending_actions
        self._reconciled = reconciled
        self._resolution_response = resolution_response
        self.prompts: list[str] = []
        self.resolutions: list[tuple[str, bool]] = []
        self.resume_calls = 0
        self.reconcile_calls = 0
        self.pending_group_calls = 0
        self.event_sink: BackendEventSink = lambda _events: None
        self.token_sink: TokenDeltaSink = lambda _delta: None

    @property
    def backend_id(self) -> str:
        return "recording"

    @property
    def configuration_error(self) -> str | None:
        return self._configuration_error

    @property
    def model_endpoint(self) -> str:
        return self.endpoint

    @property
    def model_profile_id(self) -> str:
        return "recording"

    @property
    def capability_tier(self) -> str:
        return "supervised"

    @property
    def credential_reference(self) -> str | None:
        return None

    @property
    def action_confirmation_mode(self) -> str:
        return "always-confirm"

    @property
    def continuation_requires_model_authorization(self) -> bool:
        return True

    def bind_runtime(
        self,
        *,
        event_sink: BackendEventSink,
        token_sink: TokenDeltaSink,
    ) -> None:
        self.event_sink = event_sink
        self.token_sink = token_sink

    def reconcile(
        self,
        *,
        session_id: str,  # noqa: ARG002
        known_source_event_ids: frozenset[str],
    ) -> tuple[BackendEvent, ...]:
        self.reconcile_calls += 1
        return tuple(
            event
            for event in self._reconciled
            if event.source_event_id not in known_source_event_ids
        )

    def pending_action_group(
        self,
        *,
        session_id: str,  # noqa: ARG002
    ) -> PendingActionGroup | None:
        self.pending_group_calls += 1
        return pending_action_group(self._pending_actions)

    def submit_turn(
        self,
        *,
        session_id: str,  # noqa: ARG002
        prompt: str,
    ) -> tuple[BackendEvent, ...]:
        self.prompts.append(prompt)
        requested = tuple(
            event.tool_call
            for event in self.response
            if isinstance(event, BackendConfirmationRequestEvent)
        )
        if requested:
            self._pending_actions = requested
        return self.response

    def resolve_confirmation(
        self,
        *,
        session_id: str,  # noqa: ARG002
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        group = pending_action_group(self._pending_actions)
        self.resolutions.append((action_group_id, approved))
        if not any(isinstance(event, BackendErrorEvent) for event in self._resolution_response):
            self._pending_actions = ()
        if self._resolution_response or group is None:
            return self._resolution_response
        return tuple(
            BackendConfirmationResolutionEvent(
                tool_call=action,
                action_group_id=group.group_id,
                approved=approved,
                source_event_id=f"recording:{action.tool_call_id}:confirmation-resolution",
            )
            for action in group.actions
        )

    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        return ()

    def resume(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        self.resume_calls += 1
        return ()

    def close(self) -> None:
        return None


class _InterruptBeforeResolutionBackend(_RecordingBackend):
    interrupt = True

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        if self.interrupt:
            self.interrupt = False
            raise OSError("simulated interruption before backend transition")
        return super().resolve_confirmation(
            session_id=session_id,
            action_group_id=action_group_id,
            approved=approved,
        )


class _InterruptAfterResolutionBackend(_RecordingBackend):
    interrupt = True

    def resolve_confirmation(
        self,
        *,
        session_id: str,
        action_group_id: str,
        approved: bool,
    ) -> tuple[BackendEvent, ...]:
        events = super().resolve_confirmation(
            session_id=session_id,
            action_group_id=action_group_id,
            approved=approved,
        )
        self._reconciled = events
        if self.interrupt:
            self.interrupt = False
            raise OSError("simulated interruption after backend transition")
        return events


class _PauseFailureBackend(_RecordingBackend):
    def pause(self, *, session_id: str) -> tuple[BackendEvent, ...]:  # noqa: ARG002
        return (
            BackendErrorEvent(
                error_code=BackendErrorCode.WORKER_STOPPED,
            ),
        )


class _FailingCloseBackend(_RecordingBackend):
    fail_close = True

    def close(self) -> None:
        if self.fail_close:
            raise RuntimeError("synthetic worker is still active")


def test_audit_export_does_not_initialize_the_agent_backend(tmp_path: Path) -> None:
    backend = _RecordingBackend(endpoint="https://model.local.invalid/v1/chat/completions")
    service = SessionService.local_default(
        tmp_path,
        session_id="session-synthetic-001",
        backend=backend,
        env={},
        clock=lambda: "2026-01-01T00:00:00Z",
    )

    result = service.handle(_command(CommandKind.AUDIT_EXPORT))

    assert result.events[-1].kind == EventKind.AUDIT_EXPORT_RECORDED
    assert backend.reconcile_calls == 0
    assert backend.pending_group_calls == 0
    assert service.store.read_audit_export()


def test_backend_shutdown_failure_retains_session_ownership(tmp_path: Path) -> None:
    backend = _FailingCloseBackend(endpoint="https://model.local.invalid/v1/chat/completions")
    service = SessionService.local_default(
        tmp_path,
        session_id="session-main",
        env={},
        backend=backend,
        clock=lambda: "2026-01-01T00:00:00Z",
    )
    service.handle(_command(CommandKind.PAUSE).model_copy(update={"session_id": "session-main"}))

    with pytest.raises(RuntimeError, match="still active"):
        service.close()

    assert service.store.writer_metadata_path.is_file()
    backend.fail_close = False
    service.close()
    assert not service.store.writer_metadata_path.exists()


def _wait_for_process_marker(process: subprocess.Popen[str], marker: Path) -> None:
    for _ in range(100):
        if marker.is_file():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"child process exited before {marker.name}\n{stdout}\n{stderr}")
        time.sleep(0.05)
    pytest.fail(f"child process did not create {marker.name}")


def _event_pair(
    *,
    session_id: str = "session-synthetic-001",
    sequence: int = 0,
    kind: EventKind = EventKind.SESSION_PAUSED,
    previous_event_hash: str | None = None,
) -> tuple[SessionEvent, AuditEvent]:
    event = SessionEvent(
        event_id=f"{session_id}-event-{sequence:06d}",
        session_id=session_id,
        sequence=sequence,
        kind=kind,
        occurred_at="2026-01-01T00:00:00Z",
        payload={"command_id": "synthetic"},
        previous_event_hash=previous_event_hash,
    )
    audit_event = AuditEvent(
        event_id=f"{session_id}-audit-{sequence:06d}",
        session_id=session_id,
        sequence=sequence,
        event_type=kind.value,
        occurred_at=event.occurred_at,
        payload={"session_event_hash": compute_session_event_hash(event)},
        previous_event_hash=previous_event_hash,
        event_hash=None,
    )
    return event, audit_event.model_copy(update={"event_hash": compute_event_hash(audit_event)})


def _write_pending_commit(
    store: FileSessionStore,
    event: SessionEvent,
    audit_event: AuditEvent,
) -> None:
    store.pending_commit_path.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.session-commit.v1",
                "session_event": event.model_dump(mode="json"),
                "audit_event": audit_event.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )


def _action_group_id(*tool_call_ids: str) -> str:
    group = pending_action_group(
        tuple(
            ProposedToolCall(
                tool_call_id=tool_call_id,
                tool_name="test-tool",
                risk="unknown",
                summary="test action",
            )
            for tool_call_id in tool_call_ids
        )
    )
    assert group is not None
    return group.group_id


def _command(
    kind: CommandKind,
    *,
    command_id: str | None = None,
    **payload: JsonValue,
) -> SessionCommand:
    return SessionCommand(
        command_id=command_id or f"command-{kind.value.replace('.', '-')}",
        session_id="session-synthetic-001",
        kind=kind,
        actor_id="human",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )
