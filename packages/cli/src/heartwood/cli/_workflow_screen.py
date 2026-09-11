# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Keyboard-first research workflow setup and revision-bound stage review."""

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from heartwood.cli._interactive import format_research_review_lines
from heartwood.gateway import SessionProjection, display_safe_text
from heartwood.schemas.workflows import (
    WorkflowCatalog,
    WorkflowDefinition,
    WorkflowRequest,
    WorkflowStart,
)


class WorkflowScreen(ModalScreen[WorkflowRequest | None]):
    """Collect inputs or return a command from the exact displayed projection."""

    CSS = """
    WorkflowScreen { align: center middle; background: $background 70%; }
    #workflow-dialog {
        width: 86; max-width: 94%; height: auto; max-height: 92%;
        padding: 1 2; border: round $primary; background: $surface;
    }
    #workflow-dialog Static, #workflow-dialog Label { height: auto; margin-bottom: 1; }
    #workflow-fields { height: auto; }
    #workflow-dialog Input { margin-bottom: 1; }
    #workflow-actions { height: auto; max-height: 12; }
    """
    BINDINGS: ClassVar = [("escape", "cancel", "Close")]

    def __init__(self, catalog: WorkflowCatalog, projection: SessionProjection) -> None:
        super().__init__()
        self.catalog = catalog
        self.projection = projection
        self.definition: WorkflowDefinition | None = None

    def compose(self) -> ComposeResult:
        """Use shared metadata for labels, required inputs, and available decisions."""
        with VerticalScroll(id="workflow-dialog"):
            yield Static("Research Workflow")
            run = self.projection.workflow
            if run is not None:
                definition = next(
                    entry.definition
                    for entry in self.catalog.workflows
                    if entry.definition.workflow_id == run.binding.workflow_id
                )
                yield Static(
                    display_safe_text(
                        f"{definition.label}\n{definition.stage(run.stage_id).label}: {run.phase}",
                        preserve_newlines=True,
                    ),
                    markup=False,
                )
                if run.evaluation is not None:
                    yield Static(
                        "\n".join(
                            display_safe_text(f"{check.check_id}: {check.status}")
                            for check in run.evaluation.checks
                        ),
                        markup=False,
                    )
                yield Static(
                    "Artifacts: " + display_safe_text(run.binding.output_directory), markup=False
                )
                if review_lines := format_research_review_lines(run.research_review):
                    yield Static("\n".join(review_lines), markup=False)
                if run.phase == "review":
                    yield Static(
                        "Inspect the stage artifacts before accepting. "
                        "Acceptance does not approve future tool actions."
                    )
                yield OptionList(
                    *(
                        Option(display_safe_text(control.label), id=control.control_id)
                        for control in self.projection.workflow_controls
                    ),
                    id="workflow-actions",
                    markup=False,
                )
                if not self.projection.workflow_controls:
                    yield Static(
                        "No stage actions are available. "
                        "Resolve agent actions or wait for active work to settle."
                    )
            elif self.projection.conversation:
                yield Static(
                    "Start a research workflow in a new session to keep its evidence separate."
                )
            else:
                choices = [
                    (entry.definition.label, entry.definition.workflow_id)
                    for entry in self.catalog.workflows
                    if entry.available
                ]
                if choices:
                    yield Select(
                        choices, value=choices[0][1], allow_blank=False, id="workflow-choice"
                    )
                    yield VerticalScroll(id="workflow-fields")
                    yield Label("Output Folder")
                    yield Input(value="results", id="workflow-output")
                    yield Static(id="workflow-error", markup=False)
                    yield Button("Start Workflow", id="workflow-start", variant="primary")
                else:
                    yield Static("No research workflows are available.")

    async def on_select_changed(self, event: Select.Changed) -> None:
        """Replace the form from the selected definition, without starting an agent."""
        if event.select.id != "workflow-choice":
            return
        self.definition = next(
            entry.definition
            for entry in self.catalog.workflows
            if entry.definition.workflow_id == event.value
        )
        fields = self.query_one("#workflow-fields", VerticalScroll)
        await fields.remove_children()
        for index, item in enumerate(self.definition.inputs):
            await fields.mount(
                Label(display_safe_text(item.label)),
                Input(
                    placeholder=display_safe_text(item.description), id=f"workflow-input-{index}"
                ),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return explicit setup inputs for the existing gateway command path."""
        if event.button.id != "workflow-start" or self.definition is None:
            return
        inputs = {
            item.input_id: self.query_one(f"#workflow-input-{index}", Input).value.strip()
            for index, item in enumerate(self.definition.inputs)
        }
        output = self.query_one("#workflow-output", Input).value.strip()
        if not output or not all(inputs.values()):
            self.query_one("#workflow-error", Static).update(
                "Provide every required input and an output folder."
            )
            return
        try:
            request = WorkflowStart(
                action="start",
                workflow_id=self.definition.workflow_id,
                inputs=inputs,
                output_directory=output,
            )
        except ValueError:
            self.query_one("#workflow-error", Static).update(
                "An input is too long or invalid. Check the paths and research question."
            )
            return
        self.dismiss(request)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Keep review bound to this screen's original state even if work changes elsewhere."""
        if event.option_list.id == "workflow-actions":
            self.dismiss(self.projection.workflow_controls[event.option_index].request)

    def action_cancel(self) -> None:
        """Close without performing an action."""
        self.dismiss(None)
