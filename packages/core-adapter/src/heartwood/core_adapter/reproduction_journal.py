# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Workflow reproduction observations linked to authoritative session events."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood.core_adapter._facade import PendingActionGroup
from heartwood.core_adapter.reproduction import ReproductionWitness
from heartwood.core_adapter.research_workflows import workflow_reproduction_spec
from heartwood.schemas.workflows import WorkflowRun
from heartwood.session import EventKind, SessionEvent


class ReproductionInspector(Protocol):
    """Bounded project inspection; no tool execution or permission decisions."""

    @property
    def project_directory(self) -> str:
        """Return the expected working directory for the reviewed invocation."""

    def read_files(self, paths: tuple[str, ...]) -> dict[str, str]:
        """Return only completely available text files."""

    def is_absent(self, path: str) -> bool:
        """Establish destination absence without following symbolic links."""


class JournaledReproduction(BaseModel):
    """An observation with links to its preparation and actual tool execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    witness: ReproductionWitness
    group_id: str = Field(min_length=1)
    project_directory: str = Field(min_length=1)
    preparation_event_id: str | None = None
    execution_event_id: str | None = None

    @model_validator(mode="after")
    def validate_links(self) -> Self:
        """Require both source links for an observed outcome and neither for preparation."""
        links = (self.preparation_event_id, self.execution_event_id)
        if self.witness.status == "prepared":
            valid = links == (None, None)
        else:
            valid = all(isinstance(link, str) and link for link in links)
        if not valid:
            raise ValueError("Reproduction observation has invalid source links")
        return self


def prepare_reproduction(
    run: WorkflowRun,
    *,
    session_id: str,
    group: PendingActionGroup,
    inspector: ReproductionInspector,
) -> JournaledReproduction | None:
    """Capture a separate exact proposal while protecting bound and accepted artifacts."""
    if run.phase != "running" or len(group.actions) != 1:
        return None
    spec = workflow_reproduction_spec(run.binding, run.stage_id)
    action = group.actions[0]
    arguments = action.arguments
    command = arguments.get("command")
    if (
        spec is None
        or action.tool_name != "terminal"
        or not isinstance(command, str)
        or arguments.get("is_input", False) is not False
        or arguments.get("reset", False) is not False
        or not spec.matches_command(command)
    ):
        return None
    files = inspector.read_files(spec.protected_paths)
    witness = ReproductionWitness.prepare(
        session_id=session_id,
        run_id=run.run_id,
        stage_id=run.stage_id,
        tool_call_id=action.tool_call_id,
        spec=spec,
        command=command,
        group_size=len(group.actions),
        destination_absent=inspector.is_absent(spec.directory),
        expected=files,
        observed=inspector.read_files(spec.protected_paths),
    )
    if witness is None:
        return None
    expected = {item.value: item.sha256 for item in run.binding.inputs if item.kind == "file"}
    paths = {artifact.artifact_id: artifact.path for artifact in run.binding.artifacts}
    for accepted in run.completed:
        for artifact in accepted.artifacts:
            if artifact.artifact_id in paths:
                path = paths[artifact.artifact_id]
                if path in expected and expected[path] != artifact.sha256:
                    return None
                expected[path] = artifact.sha256
    if any(
        item.path not in expected or expected[item.path] != item.sha256
        for item in witness.protected
    ):
        return None
    return JournaledReproduction(
        witness=witness, group_id=group.group_id, project_directory=inspector.project_directory
    )


def reproduction_records(
    events: Sequence[SessionEvent], *, run_id: str, stage_id: str
) -> tuple[tuple[SessionEvent, JournaledReproduction], ...]:
    """Validate recorded transitions and source ordering before supplying stage evidence."""
    sources = {event.event_id: event for event in events}
    if len(sources) != len(events):
        raise ValueError("Reproduction source event identities are ambiguous")
    prepared: dict[str, tuple[SessionEvent, JournaledReproduction]] = {}
    latest: dict[str, tuple[SessionEvent, JournaledReproduction]] = {}
    for event in events:
        if event.kind != EventKind.WORKFLOW_EXECUTION_RECORDED:
            continue
        record = JournaledReproduction.model_validate(event.payload["observation"])
        witness = record.witness
        if witness.session_id != event.session_id:
            raise ValueError("Reproduction belongs to another session")
        if (witness.run_id, witness.stage_id) != (run_id, stage_id):
            continue
        action_id = witness.tool_call_id
        if witness.status == "prepared":
            if action_id in prepared:
                raise ValueError("Reproduction preparation was repeated")
            prepared[action_id] = (event, record)
        else:
            previous = prepared.get(action_id)
            execution = sources.get(record.execution_event_id or "")
            if previous is None or execution is None:
                raise ValueError("Reproduction source event is missing")
            before, initial = previous
            if (
                record.preparation_event_id != before.event_id
                or not before.sequence < execution.sequence < event.sequence
                or execution.session_id != event.session_id
                or execution.kind != EventKind.TOOL_EXECUTION_RECORDED
                or execution.payload.get("tool_call_id") != action_id
                or execution.payload.get("tool_name") != "terminal"
                or initial.group_id != record.group_id
                or initial.project_directory != record.project_directory
                or witness.spec != initial.witness.spec
                or witness.protected != initial.witness.protected
                or latest[action_id][1].witness.status != "prepared"
            ):
                raise ValueError("Reproduction source identity or ordering changed")
            if witness.status == "succeeded" and (
                type(execution.payload.get("exit_code")) is not int
                or execution.payload.get("exit_code") != 0
                or execution.payload.get("working_directory") != record.project_directory
                or not _approved(events, before, execution, record.group_id, action_id)
            ):
                raise ValueError("Successful reproduction requires approved successful execution")
        latest[action_id] = (event, record)
    return tuple(latest.values())


def observe_reproduction(
    run: WorkflowRun,
    *,
    events: Sequence[SessionEvent],
    execution: SessionEvent,
    inspector: ReproductionInspector,
) -> JournaledReproduction | None:
    """Capture outputs at live completion only; callers must never invoke this on replay."""
    if run.phase != "running":
        return None
    for before, record in reproduction_records(events, run_id=run.run_id, stage_id=run.stage_id):
        witness = record.witness
        if witness.status != "prepared" or witness.tool_call_id != execution.payload.get(
            "tool_call_id"
        ):
            continue
        exit_code = execution.payload.get("exit_code")
        observed = witness.observe(
            tool_call_id=witness.tool_call_id,
            approved=(
                _approved(events, before, execution, record.group_id, witness.tool_call_id)
                and execution.payload.get("working_directory") == record.project_directory
            ),
            exit_code=exit_code if type(exit_code) is int else 1,
            protected=inspector.read_files(witness.spec.protected_paths),
            outputs=inspector.read_files(witness.spec.output_paths),
        )
        return JournaledReproduction(
            witness=observed,
            group_id=record.group_id,
            project_directory=record.project_directory,
            preparation_event_id=before.event_id,
            execution_event_id=execution.event_id,
        )
    return None


def _approved(
    events: Sequence[SessionEvent],
    before: SessionEvent,
    execution: SessionEvent,
    group_id: str,
    tool_call_id: str,
) -> bool:
    for event in reversed(events):
        if (
            event.kind == EventKind.APPROVAL_RECORDED
            and event.session_id == before.session_id
            and before.sequence < event.sequence < execution.sequence
            and event.payload.get("group_id") == group_id
        ):
            return (
                event.payload.get("tool_call_ids") == [tool_call_id]
                and event.payload.get("decision") == "approved"
            )
    return False
