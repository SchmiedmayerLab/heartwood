# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Textual terminal interface for a Heartwood conversation."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from heartwood.cli._interactive import InteractionResult, InteractiveSession
from heartwood.session import SessionEvent


class HeartwoodTerminalApp(App[None]):
    """Interactive terminal adapter over one gateway-owned session."""

    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #status.working { color: $warning; }
    #status.waiting { color: $warning; text-style: bold; }
    #status.error { color: $error; }
    #conversation { height: 1fr; padding: 1 2; }
    #approval { display: none; height: auto; margin: 0 1 1 1; border: round $warning; }
    #approval-title { height: auto; padding: 0 1; color: $warning; text-style: bold; }
    #approval-actions { height: auto; max-height: 8; padding: 0 1; }
    #approval-options { height: 4; }
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
        format_events: Callable[[Sequence[SessionEvent]], Sequence[str]],
    ) -> None:
        super().__init__()
        self.session = session
        self.format_events = format_events
        self._busy = False
        self._busy_started = 0.0
        self._frame = 0
        self._animations_enabled = "NO_COLOR" not in os.environ
        self._activity_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(f"Session {self.session.session_id} · ready", id="status")
        with Vertical():
            yield RichLog(id="conversation", wrap=True, markup=False)
            with Vertical(id="approval"):
                yield Static(id="approval-title")
                yield RichLog(id="approval-actions", wrap=True, markup=False)
                yield OptionList(
                    Option("Allow all once", id="allow"),
                    Option("Reject all", id="reject"),
                    id="approval-options",
                    markup=False,
                    compact=True,
                )
            yield Input(placeholder="Ask Heartwood or enter /help", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        """Replay the persisted conversation and focus the composer."""
        self._render_events(self.session.replay())
        self._activity_timer = self.set_interval(
            0.5, self._refresh_working_status, pause=True, name="activity"
        )
        self._sync_approval()

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
        if result.replace_transcript:
            self.query_one("#conversation", RichLog).clear()
        self._render_events(result.events)
        self._set_busy(False, failed=result.failed)
        self._sync_approval()

    def _render_events(self, events: Sequence[SessionEvent]) -> None:
        log = self.query_one("#conversation", RichLog)
        for line in self.format_events(events):
            if not line:
                continue
            style = _line_style(line) if self._animations_enabled else None
            log.write(Text(line, style=style) if style else line)

    def _set_busy(self, busy: bool, *, failed: bool = False) -> None:
        self._busy = busy
        composer = self.query_one("#composer", Input)
        composer.disabled = busy
        self.query_one("#approval-options", OptionList).disabled = busy
        status = self.query_one("#status", Static)
        status.remove_class("working", "waiting", "error")
        timer = self._activity_timer
        if busy:
            self._busy_started = time.monotonic()
            self._frame = 0
            status.add_class("working")
            if timer is not None:
                timer.resume()
            self._refresh_working_status()
        else:
            if timer is not None:
                timer.pause()
            state = "error" if failed else "ready"
            if failed:
                status.add_class("error")
            status.update(f"Session {self.session.session_id} · {state}")
        if not busy:
            composer.focus()

    def _refresh_working_status(self) -> None:
        if not self._busy:
            return
        frames = (".", "i", "Y") if self._animations_enabled else ("*",)
        marker = frames[self._frame % len(frames)]
        self._frame += 1
        elapsed = int(time.monotonic() - self._busy_started)
        self.query_one("#status", Static).update(
            f"Working {marker} · {elapsed}s · local models can take several minutes"
        )

    def _sync_approval(self) -> None:
        actions = self.session.pending_actions()
        panel = self.query_one("#approval", Vertical)
        composer = self.query_one("#composer", Input)
        status = self.query_one("#status", Static)
        if not actions:
            panel.display = False
            composer.disabled = self._busy
            composer.placeholder = "Ask Heartwood or enter /help"
            if not self._busy:
                composer.focus()
            return
        panel.display = True
        label = "action" if len(actions) == 1 else "actions"
        self.query_one("#approval-title", Static).update(
            f"One decision applies to all {len(actions)} {label}"
        )
        action_log = self.query_one("#approval-actions", RichLog)
        action_log.clear()
        for index, action in enumerate(actions, 1):
            action_log.write(
                f"{index}. {action.summary}\n   {action.tool_name} · {action.risk.title()} risk"
            )
        composer.disabled = True
        composer.placeholder = "Resolve the action set to continue"
        status.remove_class("working", "error")
        status.add_class("waiting")
        status.update(f"Review required · {len(actions)} {label} · one decision")
        options = self.query_one("#approval-options", OptionList)
        options.disabled = self._busy
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Resolve the pending batch from the keyboard review control."""
        if event.option_list.id != "approval-options" or self._busy:
            return
        directive = "/allow" if event.option_id == "allow" else "/reject"
        self._set_busy(True)
        self._submit(directive)

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
    format_events: Callable[[Sequence[SessionEvent]], Sequence[str]],
) -> int:
    """Run the full-screen terminal client."""
    HeartwoodTerminalApp(session, format_events=format_events).run()
    return 0


def _line_style(line: str) -> str | None:
    """Return a restrained transcript style without parsing event payloads again."""
    if " Error:" in line:
        return "bold red"
    if " Agent:" in line:
        return "green"
    if " You:" in line:
        return "cyan"
    if "Action set " in line or " Action:" in line:
        return "yellow"
    if " Tool " in line:
        return "blue"
    return None


__all__ = ["HeartwoodTerminalApp", "run_terminal"]
