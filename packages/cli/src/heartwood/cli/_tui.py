# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Textual terminal interface for a Heartwood conversation."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from heartwood.cli._interactive import (
    InteractionActivity,
    InteractionResult,
    InteractiveSession,
    format_action_arguments,
    format_projection_lines,
    interaction_activity,
)
from heartwood.gateway import (
    ActionSettingsError,
    SessionProjection,
    action_mode_label,
    action_risk_label,
    action_tool_label,
)
from heartwood.schemas import (
    ActionConfirmationMode,
    ActionModeOptionResponse,
    ActionSettingsResponse,
)


class ActionModeScreen(ModalScreen[str | None]):
    """Keyboard-first action-mode chooser backed by gateway metadata."""

    CSS = """
    ActionModeScreen {
        align: center middle;
        background: $background 70%;
    }
    #action-mode-dialog {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #action-mode-title {
        height: auto;
        text-style: bold;
    }
    #action-mode-scope, #action-mode-detail, #action-mode-help {
        height: auto;
        color: $text-muted;
    }
    #action-mode-options {
        height: auto;
        max-height: 8;
        margin: 1 0;
    }
    #action-mode-detail {
        min-height: 3;
        padding: 1;
        background: $panel;
    }
    #action-mode-unavailable {
        height: auto;
        color: $warning;
    }
    #action-mode-help {
        margin-top: 1;
    }
    """
    BINDINGS: ClassVar = [("escape", "cancel", "Close")]

    def __init__(
        self,
        settings: ActionSettingsResponse,
        *,
        locked_reason: str | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.locked_reason = locked_reason
        self.modes = tuple(settings["modes"])

    def compose(self) -> ComposeResult:
        selected = self.settings["confirmation_mode"]
        with Vertical(id="action-mode-dialog"):
            yield Static("Action Review", id="action-mode-title")
            yield Static(self.settings["scope_description"], id="action-mode-scope")
            yield OptionList(
                *(
                    Option(
                        _mode_option_prompt(item, selected=selected),
                        id=item["command_value"],
                        disabled=(
                            not item["allowed"]
                            or (self.locked_reason is not None and item["mode"] != selected)
                        ),
                    )
                    for item in self.modes
                ),
                id="action-mode-options",
                markup=False,
                compact=False,
            )
            yield Static(id="action-mode-detail")
            yield Static(
                _unavailable_mode_summary(self.modes),
                id="action-mode-unavailable",
            )
            yield Static(
                self.locked_reason
                or "Use the arrow keys to compare modes, then press Enter to select one.",
                id="action-mode-help",
            )

    def on_mount(self) -> None:
        """Focus the active mode and show its complete behavior."""
        selected = self.settings["confirmation_mode"]
        options = self.query_one("#action-mode-options", OptionList)
        for index, item in enumerate(self.modes):
            if item["mode"] == selected:
                options.highlighted = index
                break
        options.focus()
        self._update_detail(options.highlighted)

    def on_option_list_option_highlighted(
        self,
        event: OptionList.OptionHighlighted,
    ) -> None:
        """Explain the currently highlighted mode."""
        if event.option_list.id == "action-mode-options":
            self._update_detail(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Return the selected public command value."""
        if event.option_list.id != "action-mode-options":
            return
        selected = self.settings["confirmation_mode"]
        item = self.modes[event.option_index]
        if item["mode"] == selected:
            self.dismiss(None)
            return
        if self.locked_reason is None and item["allowed"]:
            self.dismiss(item["command_value"])

    def action_cancel(self) -> None:
        """Close without changing the project setting."""
        self.dismiss(None)

    def _update_detail(self, index: int | None) -> None:
        if index is None or not 0 <= index < len(self.modes):
            return
        item = self.modes[index]
        detail = item["description"]
        if not item["allowed"] and item["unavailable_reason"]:
            detail = f"{detail}\n{item['unavailable_reason']}"
        self.query_one("#action-mode-detail", Static).update(detail)


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
    #approval-help { height: auto; padding: 0 1; color: $text-muted; }
    #approval-actions { height: auto; max-height: 12; padding: 0 1; }
    #approval-options { height: 4; }
    #composer { dock: bottom; margin: 0 1 1 1; }
    """
    TITLE = "Heartwood"
    BINDINGS: ClassVar = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "focus_composer", "Prompt"),
        ("ctrl+p", "command_palette", "Commands"),
        ("escape", "pause", "Pause"),
    ]

    def __init__(
        self,
        session: InteractiveSession,
    ) -> None:
        super().__init__()
        self.session = session
        self._busy = False
        self._busy_started = 0.0
        self._frame = 0
        self._activity = interaction_activity("")
        self._guidance_shown = False
        self._animations_enabled = "NO_COLOR" not in os.environ
        self._activity_timer: Timer | None = None
        self._projection_timer: Timer | None = None
        self._projection: SessionProjection | None = None
        self._projection_signature: tuple[object, ...] | None = None
        self._mode_label = self._selected_mode_label()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self._idle_status("ready"), id="status")
        with Vertical():
            yield RichLog(id="conversation", wrap=True, markup=False)
            with Vertical(id="approval"):
                yield Static(id="approval-title")
                yield Static(id="approval-help")
                yield RichLog(
                    id="approval-actions",
                    wrap=True,
                    markup=False,
                    auto_scroll=False,
                )
                yield OptionList(
                    Option("Reject", id="reject"),
                    Option("Allow Once", id="allow"),
                    id="approval-options",
                    markup=False,
                    compact=True,
                )
            yield Input(placeholder="Ask Heartwood or enter /help", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        """Render and continuously refresh the gateway-owned projection."""
        self._activity_timer = self.set_interval(
            0.5, self._refresh_working_status, pause=True, name="activity"
        )
        self._projection_timer = self.set_interval(0.25, self._sync_projection, name="projection")
        self._sync_projection()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit input without blocking terminal rendering."""
        if self._busy or not event.value.strip():
            return
        if event.value.strip() == "/permissions":
            event.input.value = ""
            self.action_show_permissions()
            return
        event.input.value = ""
        self._set_busy(True, activity=interaction_activity(event.value))
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
        if result.projection is not None:
            self._render_projection(result.projection)
        self._mode_label = self._selected_mode_label()
        self._set_busy(False, failed=result.failed)
        self._sync_projection()

    def _render_projection(self, projection: SessionProjection) -> None:
        log = self.query_one("#conversation", RichLog)
        log.clear()
        for line in format_projection_lines(
            projection,
            include_pending_review=False,
        ):
            if not line:
                continue
            style = _line_style(line) if self._animations_enabled else None
            log.write(Text(line, style=style) if style else line)
        self._projection = projection

    def _sync_projection(self) -> None:
        projection = self.session.replay()
        approval = projection.pending_approval
        signature = (
            projection.revision,
            projection.streaming_text,
            projection.lifecycle.status,
            None if approval is None else approval.group_id,
            0 if approval is None else len(approval.actions),
        )
        if signature != self._projection_signature:
            was_running = (
                self._projection is not None and self._projection.lifecycle.status == "running"
            )
            if projection.lifecycle.status == "running" and not was_running and not self._busy:
                self._busy_started = time.monotonic()
                self._frame = 0
                self._guidance_shown = False
            self._projection_signature = signature
            self._render_projection(projection)
            self._sync_approval(projection)
            self._sync_runtime_status(projection)

    def _set_busy(
        self,
        busy: bool,
        *,
        activity: InteractionActivity | None = None,
        failed: bool = False,
    ) -> None:
        self._busy = busy
        composer = self.query_one("#composer", Input)
        composer.disabled = busy
        self.query_one("#approval-options", OptionList).disabled = busy
        status = self.query_one("#status", Static)
        status.remove_class("working", "waiting", "error")
        timer = self._activity_timer
        if busy:
            if activity is not None:
                self._activity = activity
            self._busy_started = time.monotonic()
            self._frame = 0
            self._guidance_shown = False
            status.add_class("working")
            if timer is not None:
                timer.resume()
            self._refresh_working_status()
        else:
            runtime_running = (
                self._projection is not None and self._projection.lifecycle.status == "running"
            )
            if timer is not None and not runtime_running:
                timer.pause()
            state = (
                "error"
                if failed
                else (
                    self._projection.lifecycle.status if self._projection is not None else "ready"
                )
            )
            if failed:
                status.add_class("error")
            status.update(self._idle_status(state))
        if not busy:
            composer.focus()

    def _refresh_working_status(self) -> None:
        runtime_running = (
            self._projection is not None and self._projection.lifecycle.status == "running"
        )
        if not self._busy and not runtime_running:
            return
        frames = (".  ", ".. ", "...") if self._animations_enabled else ("...",)
        marker = frames[self._frame % len(frames)]
        self._frame += 1
        elapsed = int(time.monotonic() - self._busy_started)
        if elapsed < 10:
            message = f"{self._activity.label}{marker}"
        else:
            message = f"{self._activity.waiting_label}{marker} · {elapsed}s elapsed"
            if not self._guidance_shown:
                self._guidance_shown = True
                self.notify(self._activity.guidance, title="Still working", timeout=6)
        self.query_one("#status", Static).update(message)

    def _sync_runtime_status(self, projection: SessionProjection) -> None:
        if self._busy:
            return
        status = self.query_one("#status", Static)
        status.remove_class("working", "waiting", "error")
        if projection.lifecycle.status == "running":
            status.add_class("working")
            if self._activity_timer is not None:
                self._activity_timer.resume()
            self._activity = interaction_activity("")
            self._refresh_working_status()
        elif projection.lifecycle.status == "error":
            status.add_class("error")
            status.update(f"Session {self.session.session_id} · error")
        elif projection.pending_approval is None:
            if self._activity_timer is not None:
                self._activity_timer.pause()
            status.update(f"Session {self.session.session_id} · {projection.lifecycle.status}")

    def _sync_approval(self, projection: SessionProjection) -> None:
        approval = projection.pending_approval
        panel = self.query_one("#approval", Vertical)
        composer = self.query_one("#composer", Input)
        status = self.query_one("#status", Static)
        if approval is None:
            panel.display = False
            composer.disabled = self._busy
            composer.placeholder = "Ask Heartwood or enter /help"
            if not self._busy:
                composer.focus()
            return
        actions = approval.actions
        panel.display = True
        label = "action" if len(actions) == 1 else "actions"
        self.query_one("#approval-title", Static).update(
            f"Review Agent Actions · {len(actions)} {label.title()}"
        )
        self.query_one("#approval-help", Static).update(
            "These actions were proposed together. One decision applies to the complete set."
        )
        action_log = self.query_one("#approval-actions", RichLog)
        action_log.clear()
        for index, action in enumerate(actions, 1):
            risk_label, risk_style = _risk_presentation(action.risk or "unknown")
            heading = Text()
            heading.append(
                f"{index}. {action.summary or action_tool_label(action.tool_name)}\n",
                style="bold",
            )
            heading.append(f"   {action_tool_label(action.tool_name)} · ", style="dim")
            heading.append(risk_label, style=risk_style)
            details: list[Text | str] = [heading]
            if action.arguments:
                details.extend(
                    (
                        "   Exact arguments:",
                        *(f"     {line}" for line in format_action_arguments(action.arguments)),
                    )
                )
            for detail in details:
                action_log.write(detail)
        action_log.scroll_home(animate=False)
        composer.disabled = True
        composer.placeholder = "Resolve the action set to continue"
        status.remove_class("working", "error")
        status.add_class("waiting")
        status.update(f"Review required · {len(actions)} {label} · one decision")
        options = self.query_one("#approval-options", OptionList)
        count_label = f"{len(actions)} {label.title()}"
        options.replace_option_prompt("reject", f"Reject {count_label}")
        options.replace_option_prompt("allow", f"Allow {count_label} Once")
        options.disabled = self._busy
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Resolve the pending batch from the keyboard review control."""
        if event.option_list.id != "approval-options" or self._busy:
            return
        directive = "/allow" if event.option_id == "allow" else "/reject"
        self._set_busy(True, activity=interaction_activity(directive))
        self._submit(directive)

    def action_focus_composer(self) -> None:
        """Focus the prompt input."""
        if self.session.pending_approval() is not None:
            self.query_one("#approval-options", OptionList).focus()
            return
        self.query_one("#composer", Input).focus()

    def action_pause(self) -> None:
        """Pause active agent work through the shared command contract."""
        if self._busy:
            self.notify("The active OpenHands turn cannot yet be interrupted.", severity="warning")
            return
        if self.session.pending_approval() is not None:
            self.notify(
                "Review the pending action set before pausing.",
                severity="warning",
            )
            self.query_one("#approval-options", OptionList).focus()
            return
        self._set_busy(True, activity=interaction_activity("/pause"))
        self._submit("/pause")

    def action_show_permissions(self) -> None:
        """Open the shared action-mode chooser."""
        projection = self.session.replay()
        locked_reason = (
            "Wait for the active request to finish before changing this setting."
            if self._busy
            else (
                "Resolve the pending action set before changing this setting."
                if projection.pending_approval is not None
                else (
                    "Wait for the active task to reach a review point before changing this setting."
                    if projection.lifecycle.status == "running"
                    else None
                )
            )
        )
        try:
            settings = self.session.action_settings()
        except ActionSettingsError as error:
            self.notify(str(error), title="Action Review", severity="error")
            return
        locked_reason = locked_reason or settings["change_blocked_reason"]
        self.push_screen(
            ActionModeScreen(
                settings,
                locked_reason=locked_reason,
            ),
            self._action_mode_selected,
        )

    def action_show_status(self) -> None:
        """Show model, policy, and action-review status."""
        self._run_directive("/status")

    def action_replay(self) -> None:
        """Reload the persisted conversation."""
        self._run_directive("/replay")

    def action_export_audit(self) -> None:
        """Create the session audit export."""
        self._run_directive("/audit-export")

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        """Add Heartwood workflows to Textual's built-in command palette."""
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Action Review",
            "Choose how Heartwood confirms proposed actions",
            self.action_show_permissions,
        )
        yield SystemCommand(
            "Session Status",
            "Show the active model, policy, and action-review mode",
            self.action_show_status,
        )
        yield SystemCommand(
            "Replay Conversation",
            "Reload the durable session history",
            self.action_replay,
        )
        yield SystemCommand(
            "Export Audit",
            "Create a content-minimized audit export",
            self.action_export_audit,
        )

    def _action_mode_selected(self, command_value: str | None) -> None:
        if command_value is None:
            return
        self._set_busy(True, activity=interaction_activity("/permissions"))
        self._select_action_mode(command_value)

    @work(thread=True, exclusive=True)
    def _select_action_mode(self, command_value: str) -> None:
        try:
            settings = self.session.select_action_mode(command_value)
        except Exception as error:
            self.call_from_thread(self._finish_action_mode_selection, None, error)
        else:
            self.call_from_thread(self._finish_action_mode_selection, settings, None)

    def _finish_action_mode_selection(
        self,
        settings: ActionSettingsResponse | None,
        error: Exception | None,
    ) -> None:
        if error is not None:
            self._set_busy(False, failed=True)
            self.notify(str(error), title="Action Review", severity="error")
            return
        assert settings is not None
        self._mode_label = action_mode_label(settings["confirmation_mode"])
        self._set_busy(False)
        self.notify(
            f"Using {self._mode_label} for future action sets.",
            title="Action Review",
        )

    def _run_directive(self, directive: str) -> None:
        if self._busy:
            self.notify("Wait for the active request to finish.", severity="warning")
            return
        self._set_busy(True, activity=interaction_activity(directive))
        self._submit(directive)

    def _selected_mode_label(self) -> str:
        try:
            return action_mode_label(self.session.action_settings()["confirmation_mode"])
        except Exception:
            return "Action Review Unavailable"

    def _idle_status(self, state: str) -> str:
        return f"Session {self.session.session_id} · {state.title()} · {self._mode_label}"


def run_terminal(
    session: InteractiveSession,
) -> int:
    """Run the full-screen terminal client."""
    HeartwoodTerminalApp(session).run()
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


def _mode_option_prompt(
    item: ActionModeOptionResponse,
    *,
    selected: ActionConfirmationMode,
) -> Text:
    prompt = Text()
    prompt.append("● " if item["mode"] == selected else "  ", style="green")
    prompt.append(item["label"], style="bold")
    if item["recommended"]:
        prompt.append(" · Recommended", style="green")
    if not item["allowed"]:
        prompt.append(" · Unavailable", style="dim")
    return prompt


def _unavailable_mode_summary(modes: Sequence[ActionModeOptionResponse]) -> str:
    lines = [
        f"{item['label']} unavailable: {item['unavailable_reason']}"
        for item in modes
        if not item["allowed"]
    ]
    return "\n".join(lines)


def _risk_presentation(risk: str) -> tuple[str, str]:
    style = {
        "high": "bold red",
        "low": "green",
        "medium": "yellow",
        "unknown": "bold yellow",
    }.get(risk.lower(), "bold yellow")
    return action_risk_label(risk), style


__all__ = ["ActionModeScreen", "HeartwoodTerminalApp", "run_terminal"]
