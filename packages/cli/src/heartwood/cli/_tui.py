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
from pathlib import PurePosixPath
from typing import ClassVar, cast

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import (
    Footer,
    Header,
    Input,
    OptionList,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from heartwood.cli._interactive import (
    InteractionActivity,
    InteractionResult,
    InteractiveSession,
    format_action_arguments,
    format_conversation_lines,
    format_runtime_lines,
    interaction_activity,
)
from heartwood.cli._workspace_presentation import (
    format_workspace_changes,
    format_workspace_diff,
    format_workspace_file,
)
from heartwood.gateway import (
    ActionSettingsError,
    SessionProjection,
    action_mode_label,
    action_risk_label,
    action_tool_label,
)
from heartwood.gateway import (
    display_safe_text as terminal_safe_text,
)
from heartwood.schemas import (
    ActionConfirmationMode,
    ActionModeOptionResponse,
    ActionSettingsResponse,
    WorkspaceChangesResponse,
    WorkspaceDiffResponse,
    WorkspaceFileResponse,
    WorkspaceTreeResponse,
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
    #workspace-tabs, TabPane { height: 1fr; }
    #conversation-pane { padding: 0; }
    #conversation { height: 1fr; padding: 1 2; }
    #streaming {
        display: none;
        height: auto;
        max-height: 6;
        padding: 0 2 1 2;
        color: $success;
    }
    #projection-details {
        display: none;
        height: auto;
        max-height: 8;
        padding: 0 2 1 2;
        color: $text-muted;
    }
    #approval { display: none; height: auto; margin: 0 1 1 1; border: round $warning; }
    #approval-title { height: auto; padding: 0 1; color: $warning; text-style: bold; }
    #approval-help { height: auto; padding: 0 1; color: $text-muted; }
    #approval-actions { height: auto; max-height: 12; padding: 0 1; }
    #approval-options { height: 4; }
    #composer { dock: bottom; margin: 0 1 1 1; }
    .workspace-split { height: 1fr; }
    #file-tree, #change-list { width: 36%; min-width: 28; border-right: solid $panel; }
    #file-preview, #change-preview { width: 1fr; padding: 1 2; }
    """
    TITLE = "Heartwood"
    BINDINGS: ClassVar = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "focus_composer", "Prompt"),
        ("ctrl+p", "command_palette", "Commands"),
        ("ctrl+1", "show_conversation", "Conversation"),
        ("ctrl+2", "show_files", "Files"),
        ("ctrl+3", "show_changes", "Changes"),
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
        self._projection_read_in_flight = False
        self._rendered_sequence: int | None = None
        self._retired_stream_epochs: set[str] = set()
        self._mode_label = "Action Review"
        self._workspace_revision: int | None = None
        self._workspace_overview_generation = 0
        self._workspace_file_generation = 0
        self._workspace_diff_generation = 0
        self._change_paths: dict[str, str] = {}
        self._selected_file_path: str | None = None
        self._selected_change_path: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self._idle_status("ready"), id="status")
        with TabbedContent(initial="conversation-pane", id="workspace-tabs"):
            with TabPane("Conversation", id="conversation-pane"):
                yield RichLog(id="conversation", wrap=True, markup=False)
                yield Static(id="streaming", markup=False)
                yield RichLog(id="projection-details", wrap=True, markup=False)
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
            with TabPane("Files", id="files-pane"), Horizontal(classes="workspace-split"):
                yield Tree[str]("Project", id="file-tree")
                yield RichLog(
                    id="file-preview",
                    wrap=False,
                    markup=False,
                )
            with TabPane("Changes", id="changes-pane"), Horizontal(classes="workspace-split"):
                yield OptionList(id="change-list", markup=False, compact=True)
                yield RichLog(
                    id="change-preview",
                    wrap=False,
                    markup=False,
                )
        yield Footer()

    def on_mount(self) -> None:
        """Render and continuously refresh the gateway-owned projection."""
        self._activity_timer = self.set_interval(
            0.5, self._refresh_working_status, pause=True, name="activity"
        )
        self._projection_timer = self.set_interval(0.25, self._sync_projection, name="projection")
        self.query_one("#composer", Input).focus()
        self._sync_projection()
        self._load_action_settings(show_screen=False)

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
            self.query_one("#conversation", RichLog).write(
                terminal_safe_text(result.message, preserve_newlines=True)
            )
        if result.replace_transcript:
            self.query_one("#conversation", RichLog).clear()
        self._set_busy(False, failed=result.failed)
        if result.projection is not None:
            self._apply_projection(
                result.projection,
                force=result.replace_transcript,
            )
        self._sync_projection()
        self._load_action_settings(show_screen=False)

    def _render_projection(
        self,
        projection: SessionProjection,
        *,
        reset_transcript: bool,
    ) -> None:
        log = self.query_one("#conversation", RichLog)
        if reset_transcript:
            log.clear()
            self._rendered_sequence = None
        for line in format_conversation_lines(
            projection,
            after_sequence=self._rendered_sequence,
        ):
            if not line:
                continue
            style = _line_style(line) if self._animations_enabled else None
            log.write(Text(line, style=style) if style else line)
        if projection.conversation:
            self._rendered_sequence = max(message.sequence for message in projection.conversation)
        self._sync_streaming_text(projection)
        self._sync_projection_details(projection)
        self._projection = projection

    def _sync_streaming_text(self, projection: SessionProjection) -> None:
        streaming = self.query_one("#streaming", Static)
        text = terminal_safe_text(
            projection.streaming_text,
            preserve_newlines=True,
        )
        streaming.display = bool(text)
        if not text:
            streaming.update("")
            return
        line = f"Agent: {text}"
        streaming.update(Text(line, style="green") if self._animations_enabled else line)

    def _sync_projection_details(self, projection: SessionProjection) -> None:
        details = self.query_one("#projection-details", RichLog)
        lines = format_runtime_lines(projection)
        details.clear()
        details.display = bool(lines)
        for line in lines:
            details.write(line)

    def _transcript_requires_reset(self, projection: SessionProjection) -> bool:
        cursor = self._rendered_sequence
        return cursor is not None and not any(
            message.sequence == cursor for message in projection.conversation
        )

    def _sync_projection(self) -> None:
        if self._projection_read_in_flight:
            return
        self._projection_read_in_flight = True
        self._read_projection()

    @work(thread=True, group="projection-read")
    def _read_projection(self) -> None:
        try:
            projection = self.session.replay()
        except Exception:
            self.call_from_thread(self._finish_projection_read, None)
            return
        self.call_from_thread(self._finish_projection_read, projection)

    def _finish_projection_read(self, projection: SessionProjection | None) -> None:
        self._projection_read_in_flight = False
        if projection is not None:
            self._apply_projection(projection)

    def _apply_projection(
        self,
        projection: SessionProjection,
        *,
        force: bool = False,
    ) -> None:
        current = self._projection
        epoch_changed = False
        if current is not None and projection.stream_epoch != current.stream_epoch:
            epoch_changed = True
            if projection.stream_epoch in self._retired_stream_epochs:
                return
            self._retired_stream_epochs.add(current.stream_epoch)
        if (
            current is not None
            and not epoch_changed
            and (projection.revision, projection.stream_revision)
            < (current.revision, current.stream_revision)
        ):
            return
        approval = projection.pending_approval
        signature = (
            projection.revision,
            projection.stream_revision,
            projection.stream_epoch,
            projection.streaming_text,
            projection.lifecycle.status,
            None if approval is None else approval.group_id,
            0 if approval is None else len(approval.actions),
        )
        if force or signature != self._projection_signature:
            was_running = (
                self._projection is not None and self._projection.lifecycle.status == "running"
            )
            if projection.lifecycle.status == "running" and not was_running and not self._busy:
                self._busy_started = time.monotonic()
                self._frame = 0
                self._guidance_shown = False
            self._projection_signature = signature
            self._render_projection(
                projection,
                reset_transcript=(
                    force
                    or current is None
                    or epoch_changed
                    or self._transcript_requires_reset(projection)
                ),
            )
            self._sync_approval(projection)
            self._sync_runtime_status(projection)
        else:
            self._projection = projection
        if projection.workspace_revision != self._workspace_revision:
            self._workspace_revision = projection.workspace_revision
            self._request_workspace_overview()

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
            if self._projection is not None and self._projection.pending_approval is not None:
                self._sync_approval(self._projection)
            else:
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
            risk_label, risk_style = _risk_presentation(action.risk)
            summary = terminal_safe_text(action.summary or action_tool_label(action.tool_name))
            tool_label = terminal_safe_text(action_tool_label(action.tool_name))
            heading = Text()
            heading.append(
                f"{index}. {summary}\n",
                style="bold",
            )
            heading.append(f"   {tool_label} · ", style="dim")
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
        self.action_show_conversation()
        if self._projection is not None and self._projection.pending_approval is not None:
            self.query_one("#approval-options", OptionList).focus()
            return
        self.query_one("#composer", Input).focus()

    def action_show_conversation(self) -> None:
        """Show the conversation and action review."""
        self.query_one("#workspace-tabs", TabbedContent).active = "conversation-pane"

    def action_show_files(self) -> None:
        """Show the bounded project tree and file viewer."""
        self.query_one("#workspace-tabs", TabbedContent).active = "files-pane"
        self.query_one("#file-tree", Tree).focus()

    def action_show_changes(self) -> None:
        """Show changed paths and read-only diffs."""
        self.query_one("#workspace-tabs", TabbedContent).active = "changes-pane"
        self.query_one("#change-list", OptionList).focus()

    def _request_workspace_overview(self) -> None:
        self._workspace_overview_generation += 1
        generation = self._workspace_overview_generation
        file_preview = self.query_one("#file-preview", RichLog)
        file_preview.clear()
        file_preview.write(
            f"Refreshing {self._selected_file_path}..."
            if self._selected_file_path is not None
            else "Loading project files..."
        )
        change_preview = self.query_one("#change-preview", RichLog)
        change_preview.clear()
        change_preview.write(
            f"Refreshing {self._selected_change_path}..."
            if self._selected_change_path is not None
            else "Loading project changes..."
        )
        self._load_workspace_overview(generation)

    @work(thread=True, group="workspace-overview", exclusive=True)
    def _load_workspace_overview(self, generation: int) -> None:
        try:
            tree = self.session.workspace_tree()
            changes = self.session.workspace_changes()
        except Exception as error:
            self.call_from_thread(
                self._finish_workspace_overview,
                None,
                None,
                error,
                generation,
            )
        else:
            self.call_from_thread(
                self._finish_workspace_overview,
                tree,
                changes,
                None,
                generation,
            )

    def _finish_workspace_overview(
        self,
        tree: WorkspaceTreeResponse | None,
        changes: WorkspaceChangesResponse | None,
        error: Exception | None,
        generation: int,
    ) -> None:
        if generation != self._workspace_overview_generation:
            return
        if error is not None:
            message = (
                "Workspace inspection unavailable: "
                f"{terminal_safe_text(error, preserve_newlines=True)}"
            )
            self.query_one("#file-preview", RichLog).clear().write(message)
            self.query_one("#change-preview", RichLog).clear().write(message)
        else:
            assert tree is not None
            assert changes is not None
            self._render_workspace_tree(tree)
            self._render_workspace_changes(changes)

    def _render_workspace_tree(self, response: WorkspaceTreeResponse) -> None:
        tree = cast(Tree[str], self.query_one("#file-tree", Tree))
        tree.root.remove_children()
        nodes: dict[str, TreeNode[str]] = {".": tree.root}
        file_nodes: dict[str, TreeNode[str]] = {}
        for entry in response["entries"]:
            path = entry["path"]
            parent_path = PurePosixPath(path).parent.as_posix()
            parent = nodes.get(parent_path, tree.root)
            label = terminal_safe_text(entry["name"])
            if entry["kind"] == "directory":
                nodes[path] = parent.add(Text(label), expand=True)
            elif entry["kind"] == "file":
                file_nodes[path] = parent.add_leaf(Text(label), data=path)
            else:
                unavailable = Text(label)
                unavailable.append(" [unavailable]", style="dim")
                parent.add_leaf(unavailable)
        tree.root.expand()
        tree.root.label = f"Project files{' · truncated' if response['truncated'] else ''}"
        if (
            self._selected_file_path is not None
            and (selected := file_nodes.get(self._selected_file_path)) is not None
        ):
            tree.select_node(selected)
            self._show_workspace_file(self._selected_file_path)
        elif self._selected_file_path is not None:
            self._selected_file_path = None
            self.query_one("#file-preview", RichLog).clear().write(
                "The previously selected file is no longer available."
            )

    def _render_workspace_changes(self, response: WorkspaceChangesResponse) -> None:
        changes = self.query_one("#change-list", OptionList)
        changes.clear_options()
        self._change_paths.clear()
        marker = {"added": "A", "deleted": "D", "modified": "M"}
        for index, change in enumerate(response["changes"]):
            option_id = f"change-{index}"
            self._change_paths[option_id] = change["path"]
            changes.add_option(
                Option(
                    Text(f"{marker[change['status']]}  {terminal_safe_text(change['path'])}"),
                    id=option_id,
                )
            )
        if not response["changes"]:
            changes.add_option(Option("(no changes available)", disabled=True))
        preview = self.query_one("#change-preview", RichLog)
        preview.clear()
        for line in format_workspace_changes(response):
            preview.write(line)
        selected_option = next(
            (
                option_id
                for option_id, path in self._change_paths.items()
                if path == self._selected_change_path
            ),
            None,
        )
        if selected_option is not None:
            changes.highlighted = int(selected_option.removeprefix("change-"))
            assert self._selected_change_path is not None
            self._show_workspace_diff(self._selected_change_path)
        elif self._selected_change_path is not None:
            self._selected_change_path = None
            preview.clear().write("The previously selected change is no longer available.")

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        """Load a selected project file through the bounded gateway service."""
        if event.control.id == "file-tree" and event.node.data is not None:
            self._show_workspace_file(event.node.data)

    def _show_workspace_file(self, path: str) -> None:
        self._selected_file_path = path
        self._workspace_file_generation += 1
        generation = self._workspace_file_generation
        preview = self.query_one("#file-preview", RichLog)
        preview.clear()
        preview.write(f"Loading {terminal_safe_text(path)}...")
        self._load_workspace_file(path, generation)

    @work(thread=True, group="workspace-file", exclusive=True)
    def _load_workspace_file(self, path: str, generation: int) -> None:
        try:
            response = self.session.workspace_file(path)
        except Exception as error:
            self.call_from_thread(
                self._finish_workspace_file,
                None,
                error,
                generation,
            )
        else:
            self.call_from_thread(
                self._finish_workspace_file,
                response,
                None,
                generation,
            )

    def _finish_workspace_file(
        self,
        response: WorkspaceFileResponse | None,
        error: Exception | None,
        generation: int,
    ) -> None:
        if generation != self._workspace_file_generation:
            return
        preview = self.query_one("#file-preview", RichLog)
        preview.clear()
        if error is not None:
            preview.write(f"File unavailable: {terminal_safe_text(error, preserve_newlines=True)}")
            return
        assert response is not None
        if response["status"] in {"available", "truncated"} and response["content"] is not None:
            try:
                lexer = Syntax.guess_lexer(response["path"], response["content"])
            except Exception:
                lexer = "text"
            preview.write(
                Syntax(
                    terminal_safe_text(response["content"], preserve_newlines=True),
                    lexer,
                    line_numbers=True,
                    word_wrap=False,
                )
            )
            if response["message"]:
                preview.write(response["message"])
            return
        for line in format_workspace_file(response):
            preview.write(line)

    def on_option_list_option_highlighted(
        self,
        event: OptionList.OptionHighlighted,
    ) -> None:
        """Preview the highlighted changed path."""
        if event.option_list.id != "change-list" or event.option_id is None:
            return
        if path := self._change_paths.get(event.option_id):
            self._show_workspace_diff(path)

    def _show_workspace_diff(self, path: str) -> None:
        self._selected_change_path = path
        self._workspace_diff_generation += 1
        generation = self._workspace_diff_generation
        preview = self.query_one("#change-preview", RichLog)
        preview.clear()
        preview.write(f"Loading {terminal_safe_text(path)}...")
        self._load_workspace_diff(path, generation)

    @work(thread=True, group="workspace-diff", exclusive=True)
    def _load_workspace_diff(self, path: str, generation: int) -> None:
        try:
            response = self.session.workspace_diff(path)
        except Exception as error:
            self.call_from_thread(
                self._finish_workspace_diff,
                None,
                error,
                generation,
            )
        else:
            self.call_from_thread(
                self._finish_workspace_diff,
                response,
                None,
                generation,
            )

    def _finish_workspace_diff(
        self,
        response: WorkspaceDiffResponse | None,
        error: Exception | None,
        generation: int,
    ) -> None:
        if generation != self._workspace_diff_generation:
            return
        preview = self.query_one("#change-preview", RichLog)
        preview.clear()
        if error is not None:
            preview.write(
                f"Change unavailable: {terminal_safe_text(error, preserve_newlines=True)}"
            )
            return
        assert response is not None
        preview.write(
            Syntax(
                "\n".join(format_workspace_diff(response)),
                "diff",
                line_numbers=False,
                word_wrap=False,
            )
        )

    def action_pause(self) -> None:
        """Pause active agent work through the shared command contract."""
        if self._busy:
            self.notify("The active OpenHands turn cannot yet be interrupted.", severity="warning")
            return
        if self._projection is not None and self._projection.pending_approval is not None:
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
        locked_reason = (
            "Wait for the active request to finish before changing this setting."
            if self._busy
            else None
        )
        self._load_action_settings(show_screen=True, locked_reason=locked_reason)

    @work(thread=True, group="action-settings-read", exclusive=True)
    def _load_action_settings(
        self,
        *,
        show_screen: bool,
        locked_reason: str | None = None,
    ) -> None:
        projection: SessionProjection | None = None
        try:
            projection = self.session.replay() if show_screen else None
            settings = self.session.action_settings()
        except ActionSettingsError as error:
            self.call_from_thread(
                self._finish_action_settings_load,
                None,
                error,
                projection=projection,
                show_screen=show_screen,
                locked_reason=locked_reason,
            )
        except Exception:
            self.call_from_thread(
                self._finish_action_settings_load,
                None,
                ActionSettingsError("Action review settings are unavailable"),
                projection=projection,
                show_screen=show_screen,
                locked_reason=locked_reason,
            )
        else:
            self.call_from_thread(
                self._finish_action_settings_load,
                settings,
                None,
                projection=projection,
                show_screen=show_screen,
                locked_reason=locked_reason,
            )

    def _finish_action_settings_load(
        self,
        settings: ActionSettingsResponse | None,
        error: ActionSettingsError | None,
        *,
        projection: SessionProjection | None,
        show_screen: bool,
        locked_reason: str | None,
    ) -> None:
        if projection is not None:
            self._apply_projection(projection)
        if error is not None:
            self._mode_label = "Action Review Unavailable"
            if show_screen:
                self.notify(str(error), title="Action Review", severity="error")
            return
        assert settings is not None
        self._mode_label = action_mode_label(settings["confirmation_mode"])
        if not self._busy:
            if self._projection is None:
                self.query_one("#status", Static).update(self._idle_status("ready"))
            else:
                self._sync_runtime_status(self._projection)
        if not show_screen:
            return
        if locked_reason is None and projection is not None:
            if projection.pending_approval is not None:
                locked_reason = "Resolve the pending action set before changing this setting."
            elif projection.lifecycle.status == "running":
                locked_reason = (
                    "Wait for the active task to reach a review point before changing this setting."
                )
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
            "Project Files",
            "Inspect the bounded project tree and text files",
            self.action_show_files,
        )
        yield SystemCommand(
            "Project Changes",
            "Inspect changed paths and read-only diffs",
            self.action_show_changes,
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
    if " Tool:" in line:
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
