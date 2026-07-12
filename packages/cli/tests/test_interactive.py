# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, RichLog, Static

from heartwood.cli import _format_event
from heartwood.cli._interactive import InteractiveSession
from heartwood.cli._tui import HeartwoodTerminalApp
from heartwood.gateway import SessionGateway


def test_interactive_session_uses_gateway_commands_and_persisted_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")
    gateway = SessionGateway(workspace=tmp_path / "sessions")
    gateway.start()
    try:
        session = InteractiveSession(gateway, session_id="terminal")

        task = session.submit("summarize the synthetic cohort")
        invalid = session.submit("/allow")
        replay = session.submit("/replay")

        assert not task.failed
        assert any("You: summarize" in _format_event(event) for event in task.events)
        assert invalid.message == "Unknown command: /allow"
        assert replay.events == session.replay()
        assert replay.replace_transcript
    finally:
        gateway.stop()


def test_textual_terminal_submits_without_blocking_and_replays_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")
    gateway = SessionGateway(workspace=tmp_path / "sessions")
    gateway.start()

    async def exercise() -> None:
        session = InteractiveSession(gateway, session_id="tui")
        app = HeartwoodTerminalApp(session, format_event=_format_event)
        async with app.run_test() as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "inspect the synthetic workspace"
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if "ready" in str(app.query_one("#status", Static).render()):
                    break

            assert not composer.disabled
            conversation = app.query_one("#conversation", RichLog)
            line_count = len(conversation.lines)
            assert line_count > 0
            assert session.replay()

            composer.value = "/replay"
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if "ready" in str(app.query_one("#status", Static).render()):
                    break
            assert len(conversation.lines) == line_count

    try:
        asyncio.run(exercise())
    finally:
        gateway.stop()
