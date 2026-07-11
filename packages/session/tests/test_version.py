# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Smoke test: the session package imports and exposes a version string."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import heartwood.session
from heartwood.session import (
    CommandKind,
    EventKind,
    SessionCommand,
    SessionEvent,
    validate_session_id,
)


def test_version_is_nonempty_string() -> None:
    """The package advertises a non-empty version string."""
    assert isinstance(heartwood.session.__version__, str)
    assert heartwood.session.__version__


def test_session_command_contract_serializes_enum_values() -> None:
    """Session commands have stable JSON-facing names."""
    command = SessionCommand(
        command_id="command-1",
        session_id="session-1",
        kind=CommandKind.DETECT,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload={"scope": "environment"},
    )
    assert command.model_dump(mode="json") == {
        "schema_version": "heartwood.session-command.v1",
        "command_id": "command-1",
        "session_id": "session-1",
        "kind": "detect",
        "actor_id": "synthetic-user",
        "created_at": "2026-01-01T00:00:00Z",
        "payload": {"scope": "environment"},
    }


def test_session_event_contract_carries_hash_chain_pointer() -> None:
    """Session events carry sequence and hash-chain context for audit export."""
    event = SessionEvent(
        event_id="event-1",
        session_id="session-1",
        sequence=0,
        kind=EventKind.DETECTION_PROPOSED,
        occurred_at="2026-01-01T00:00:01Z",
        payload={"platform": "generic", "confidence": 1.0},
        previous_event_hash=None,
    )
    assert event.kind == "detection.proposed"
    assert event.sequence == 0


@pytest.mark.parametrize("session_id", ["../escape", "invalid session", "a" * 129])
def test_session_ids_reject_unsafe_or_oversized_values(session_id: str) -> None:
    """Session identifiers are safe for persistence and transport boundaries."""
    with pytest.raises(ValueError, match="session id must start"):
        validate_session_id(session_id)

    with pytest.raises(ValidationError, match="session_id"):
        SessionCommand(
            command_id="command-1",
            session_id=session_id,
            kind=CommandKind.DETECT,
            actor_id="synthetic-user",
            created_at="2026-01-01T00:00:00Z",
        )
