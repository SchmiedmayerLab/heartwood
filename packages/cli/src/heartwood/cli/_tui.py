# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Textual terminal interface for a Heartwood conversation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from heartwood.cli._interactive import InteractionResult, InteractiveSession
from heartwood.session import SessionEvent


class HeartwoodTerminalApp(App[None]):
    """Interactive terminal adapter over one gateway-owned session."""

    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #conversation { height: 1fr; padding: 1 2; }
    #composer { dock: bottom; margin: 0 1 1 1; }
    """
    TITLE = "Heartwood"
    BINDINGS: ClassVar = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "focus_composer", "Prompt"),
        ("escape", "pause", "Pause"),
    ]

    def __init__(
        self,
        session: InteractiveSession,
        *,
        format_event: Callable[[SessionEvent], str],
    ) -> None:
        super().__init__()
        self.session = session
        self.format_event = format_event
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(f"Session {self.session.session_id} · ready", id="status")
        with Vertical():
            yield RichLog(id="conversation", wrap=True, markup=False)
            yield Input(placeholder="Ask Heartwood or enter /help", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        """Replay the persisted conversation and focus the composer."""
        self._render_events(self.session.replay())
        self.query_one("#composer", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit input without blocking terminal rendering."""
        if self._busy or not event.value.strip():
            return
        event.input.value = ""
        self._set_busy(True)
        self._submit(event.value)

    @work(thread=True, exclusive=True)
    def _submit(self, line: str) -> None:
        try:
            result = self.session.submit(line)
        except Exception as error:
            result = InteractionResult(message=f"Error: {error}", error=True)
        self.call_from_thread(self._finish_interaction, result)

    def _finish_interaction(self, result: InteractionResult) -> None:
        if result.exit_requested:
            self.exit()
            return
        if result.message:
            self.query_one("#conversation", RichLog).write(result.message)
        self._render_events(result.events)
        self._set_busy(False, failed=result.failed)

    def _render_events(self, events: Sequence[SessionEvent]) -> None:
        log = self.query_one("#conversation", RichLog)
        for event in events:
            if line := self.format_event(event):
                log.write(line)

    def _set_busy(self, busy: bool, *, failed: bool = False) -> None:
        self._busy = busy
        composer = self.query_one("#composer", Input)
        composer.disabled = busy
        state = "working" if busy else ("error" if failed else "ready")
        self.query_one("#status", Static).update(f"Session {self.session.session_id} · {state}")
        if not busy:
            composer.focus()

    def action_focus_composer(self) -> None:
        """Focus the prompt input."""
        self.query_one("#composer", Input).focus()

    def action_pause(self) -> None:
        """Pause an idle session through the shared command contract."""
        if self._busy:
            self.notify("The active OpenHands turn cannot yet be interrupted.", severity="warning")
            return
        self._set_busy(True)
        self._submit("/pause")


def run_terminal(
    session: InteractiveSession,
    *,
    format_event: Callable[[SessionEvent], str],
) -> int:
    """Run the full-screen terminal client."""
    HeartwoodTerminalApp(session, format_event=format_event).run()
    return 0


__all__ = ["HeartwoodTerminalApp", "run_terminal"]
