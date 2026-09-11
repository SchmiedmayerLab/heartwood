# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Scientific stage records bound to the existing authoritative session journal."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.schemas.experiments import (
    ExperimentDefinition,
    ExperimentEvent,
    ExperimentEvidence,
    ExperimentFile,
    reduce_experiment_events,
)
from heartwood.schemas.workflows import WorkflowRun, WorkflowStageEvaluation
from heartwood.session import EventKind, SessionEvent, compute_session_event_hash

_EVIDENCE_KINDS = {
    EventKind.TOOL_CALL_PROPOSED,
    EventKind.APPROVAL_RECORDED,
    EventKind.CONFIRMATION_RESOLVED,
    EventKind.TOOL_EXECUTION_RECORDED,
    EventKind.WORKFLOW_EXECUTION_RECORDED,
}


class WorkflowProvenanceInspector(Protocol):
    """Gateway observations, not another agent or script executor."""

    def experiment_definition(
        self, run: WorkflowRun, *, session_id: str, actor_id: str, invocation: str
    ) -> ExperimentDefinition:
        """Observe stage inputs before dispatching model work."""

    def experiment_outputs(
        self, run: WorkflowRun, evaluation: WorkflowStageEvaluation
    ) -> tuple[ExperimentFile, ...]:
        """Observe outputs matching the independently accepted stage evidence."""


def stage_experiment_id(session_id: str, run_id: str, stage_id: str) -> UUID:
    """Derive one project-scoped stage identity, stable across command retries."""
    return uuid5(NAMESPACE_URL, json.dumps(["heartwood.stage", session_id, run_id, stage_id]))


def experiment_event_fingerprint(event: ExperimentEvent) -> str:
    """Bind the exact scientific event to its containing session event."""
    return hashlib.sha256(
        json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def stage_experiment_outcome(
    events: Sequence[SessionEvent],
    run: WorkflowRun,
    *,
    status: Literal["succeeded", "cancelled"],
    at: datetime,
    outputs: tuple[ExperimentFile, ...] = (),
) -> ExperimentEvent | None:
    """Finish the exact started stage; an unstarted stage has no execution record."""
    if run.started_sequence is None:
        return None
    identity = stage_experiment_id(events[0].session_id, run.run_id, run.stage_id)
    records = workflow_experiment_events(events)
    current = next(
        (item for item in reduce_experiment_events(records) if item.run_id == identity), None
    )
    if current is None:
        raise ValueError("The stage has no recorded experiment start")
    if current.status != "started":
        raise ValueError("The stage experiment already has a terminal outcome")
    return ExperimentEvent(
        event_id=uuid5(identity, status),
        run_id=identity,
        status=status,
        at=at,
        attempt=1,
        outputs=outputs,
        evidence=tuple(
            ExperimentEvidence(
                event_id=event.event_id,
                event_sha256=compute_session_event_hash(event).removeprefix("sha256:"),
                kind=str(event.kind),
            )
            for event in events
            if event.sequence > run.started_sequence and event.kind in _EVIDENCE_KINDS
        ),
    )


def workflow_experiment_events(events: Sequence[SessionEvent]) -> tuple[ExperimentEvent, ...]:
    """Verify source links before projecting or exporting stage provenance.

    Source histories must already have passed the session store's paired audit
    verification. Replaying this function never reads project artifacts or resumes work.
    """
    records: list[ExperimentEvent] = []
    sources: dict[str, SessionEvent] = {}
    starts: dict[UUID, tuple[SessionEvent, ExperimentDefinition]] = {}
    for source in events:
        payload = source.payload.get("experiment")
        if source.kind == EventKind.WORKFLOW_UPDATED and payload is not None:
            try:
                event = ExperimentEvent.model_validate(payload)
                workflow = WorkflowRun.model_validate(source.payload["run"])
            except (KeyError, ValidationError):
                raise ValueError("Workflow experiment record is invalid") from None
            if source.payload.get("experiment_fingerprint") != experiment_event_fingerprint(event):
                raise ValueError("Workflow experiment fingerprint does not match")
            if event.at > datetime.fromisoformat(source.occurred_at):
                raise ValueError("Workflow experiment time is after its journal record")
            if event.status == "started":
                definition = event.definition
                if definition is None or definition.stage is None:
                    raise ValueError("Workflow experiment is missing its stage identity")
                stage = definition.stage
                if (
                    definition.source != "heartwood"
                    or definition.entry_point is not None
                    or stage.session_id != source.session_id
                    or stage.workflow_run_id != workflow.run_id
                    or stage.stage_id != workflow.stage_id
                    or stage.workflow_sha256 != workflow.binding.workflow_fingerprint
                    or workflow.phase != "running"
                    or workflow.started_sequence != source.sequence
                    or source.payload.get("transition") != "run"
                    or event.run_id
                    != stage_experiment_id(source.session_id, workflow.run_id, workflow.stage_id)
                    or event.event_id != uuid5(event.run_id, "started")
                    or event.evidence
                ):
                    raise ValueError("Workflow experiment start does not match its source")
                starts[event.run_id] = (source, definition)
            else:
                if event.run_id not in starts:
                    raise ValueError("Workflow experiment outcome is missing its start")
                beginning, definition = starts[event.run_id]
                outcome_stage = definition.stage
                if outcome_stage is None:
                    raise ValueError("Workflow experiment is missing its stage identity")
                if (
                    event.event_id != uuid5(event.run_id, event.status)
                    or workflow.run_id != outcome_stage.workflow_run_id
                ):
                    raise ValueError("Workflow experiment outcome identity changed")
                if event.status == "succeeded":
                    if not any(
                        item.assessment.stage_id == outcome_stage.stage_id
                        for item in workflow.completed
                    ):
                        raise ValueError("Successful experiment requires accepted stage evidence")
                elif event.status != "cancelled" or workflow.phase != "cancelled":
                    raise ValueError("Workflow experiment outcome does not match its transition")
                expected = tuple(
                    ExperimentEvidence(
                        event_id=item.event_id,
                        event_sha256=compute_session_event_hash(item).removeprefix("sha256:"),
                        kind=str(item.kind),
                    )
                    for item in sources.values()
                    if item.sequence > beginning.sequence and item.kind in _EVIDENCE_KINDS
                )
                if event.evidence != expected:
                    raise ValueError("Workflow experiment evidence links are incomplete or changed")
            _validate_artifacts(event, workflow, definition)
            records.append(event)
        sources[source.event_id] = source
    reduce_experiment_events(tuple(records))
    return tuple(records)


def _validate_artifacts(
    event: ExperimentEvent, workflow: WorkflowRun, definition: ExperimentDefinition
) -> None:
    stage_reference = definition.stage
    if stage_reference is None:
        raise ValueError("Workflow experiment has no stage")
    contract = research_workflow(workflow.binding.workflow_id)
    stage = contract.stage(stage_reference.stage_id)
    artifacts = {item.artifact_id: item for item in contract.artifacts}
    paths = {
        name: str(PurePosixPath(workflow.binding.output_directory) / item.relative_path)
        for name, item in artifacts.items()
    }
    if set(definition.output_paths) != {paths[name] for name in stage.writes} or set(
        definition.code_output_paths
    ) != {paths[name] for name in stage.writes if artifacts[name].media_type == "text/x-python"}:
        raise ValueError("Workflow experiment output contract changed")
    accepted = {
        item.artifact_id: item.sha256
        for evaluation in workflow.completed
        for item in evaluation.artifacts
    }
    if event.status == "started":
        expected = {
            item.value: item.sha256 for item in workflow.binding.inputs if item.kind == "file"
        }
        for name in stage.reads:
            if name in paths:
                if name not in accepted:
                    raise ValueError("Workflow experiment reads an unaccepted artifact")
                expected[paths[name]] = accepted[name]
        observed = {item.path: item.sha256 for item in (*definition.inputs, *definition.code)}
        if observed != expected:
            raise ValueError("Workflow experiment input fingerprints changed")
    elif event.status == "succeeded":
        expected = {paths[name]: accepted[name] for name in stage.writes if name in accepted}
        if {item.path: item.sha256 for item in event.outputs} != expected:
            raise ValueError("Workflow experiment output fingerprints changed")
