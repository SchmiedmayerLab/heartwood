# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from heartwood.core_adapter.reproduction import ReproductionSpec, ReproductionWitness
from heartwood.core_adapter.reproduction_journal import (
    JournaledReproduction,
    observe_reproduction,
    reproduction_records,
)
from heartwood.core_adapter.research_workflows import research_workflow
from heartwood.schemas import JsonValue
from heartwood.schemas.workflows import WorkflowBoundInput, WorkflowProjectBinding, WorkflowRun
from heartwood.session import EventKind, SessionEvent

_FILES = {"analysis.py": "print('synthetic')", "data.csv": "x\n1\n"}
_OUTPUTS = {"reproduced/metrics.json": "{}", "reproduced/predictions.csv": "prediction\n1\n"}


class Inspector:
    project_directory = "/synthetic/project"

    def __init__(self) -> None:
        self.files = {**_FILES, **_OUTPUTS}

    def read_files(self, paths: tuple[str, ...]) -> dict[str, str]:
        return {path: self.files[path] for path in paths if path in self.files}

    def is_absent(self, path: str) -> bool:
        return not any(name == path or name.startswith(path + "/") for name in self.files)


def _prepared() -> JournaledReproduction:
    spec = ReproductionSpec(
        program="analysis.py",
        data="data.csv",
        directory="reproduced",
        protected_paths=tuple(_FILES),
        output_names=("metrics.json", "predictions.csv"),
    )
    witness = ReproductionWitness.prepare(
        session_id="research",
        run_id="run",
        stage_id="verify",
        tool_call_id="action",
        spec=spec,
        command=spec.command,
        group_size=1,
        destination_absent=True,
        expected=_FILES,
        observed=_FILES,
    )
    assert witness is not None
    return JournaledReproduction(
        witness=witness, group_id="group", project_directory=Inspector.project_directory
    )


def _event(sequence: int, kind: EventKind, payload: dict[str, JsonValue]) -> SessionEvent:
    return SessionEvent(
        event_id=f"event-{sequence}",
        session_id="research",
        sequence=sequence,
        kind=kind,
        occurred_at="2026-09-11T00:00:00Z",
        payload=payload,
    )


def _record(sequence: int, observation: JournaledReproduction) -> SessionEvent:
    return _event(
        sequence,
        EventKind.WORKFLOW_EXECUTION_RECORDED,
        {"observation": cast(dict[str, JsonValue], observation.model_dump(mode="json"))},
    )


def _events() -> list[SessionEvent]:
    initial = _prepared()
    observed = JournaledReproduction(
        witness=initial.witness.observe(
            tool_call_id="action", approved=True, exit_code=0, protected=_FILES, outputs=_OUTPUTS
        ),
        group_id="group",
        project_directory=Inspector.project_directory,
        preparation_event_id="event-0",
        execution_event_id="event-2",
    )
    return [
        _record(0, initial),
        _event(
            1,
            EventKind.APPROVAL_RECORDED,
            {"group_id": "group", "tool_call_ids": ["action"], "decision": "approved"},
        ),
        _event(
            2,
            EventKind.TOOL_EXECUTION_RECORDED,
            {
                "tool_call_id": "action",
                "tool_name": "terminal",
                "exit_code": 0,
                "working_directory": Inspector.project_directory,
            },
        ),
        _record(3, observed),
    ]


def _run() -> WorkflowRun:
    definition = research_workflow("baseline-analysis")
    # Only the observation's run/phase identity is exercised in these journal unit tests.
    return WorkflowRun(
        run_id="run",
        revision=0,
        phase="running",
        stage_id="verify",
        created_at=datetime.now(UTC),
        binding=WorkflowProjectBinding(
            workflow_id=definition.workflow_id,
            workflow_fingerprint=definition.fingerprint,
            inputs=tuple(
                WorkflowBoundInput(
                    input_id=item.input_id,
                    kind=item.kind,
                    value=f"{item.input_id}.txt",
                    sha256="a" * 64,
                )
                for item in definition.inputs
            ),
            output_directory="results",
            artifacts=definition.bind_artifacts("results"),
        ),
    )


def test_restoration_uses_linked_evidence_without_recapturing_files() -> None:
    records = reproduction_records(_events(), run_id="run", stage_id="verify")
    assert len(records) == 1
    _, record = records[0]
    assert record.witness.status == "succeeded"
    assert record.witness.verifies(protected=_FILES, outputs=_OUTPUTS)
    assert reproduction_records(_events(), run_id="other", stage_id="verify") == ()
    assert reproduction_records(_events(), run_id="run", stage_id="other") == ()


@pytest.mark.parametrize(
    "damage",
    [
        "preparation",
        "execution",
        "approval",
        "denied",
        "later-denial",
        "wrong-group",
        "multiple-actions",
        "wrong-action",
        "wrong-tool",
        "failed",
        "boolean-exit",
        "wrong-directory",
        "unknown-directory",
        "wrong-session",
        "wrong-order",
        "duplicate-event",
        "duplicate-preparation",
        "duplicate-observation",
        "changed-spec",
        "changed-protected",
        "changed-root",
    ],
)
def test_success_requires_unambiguous_ordered_source_evidence(damage: str) -> None:
    events = _events()
    key: str
    value: object
    if damage in {"preparation", "execution", "approval"}:
        events.pop({"preparation": 0, "execution": 2, "approval": 1}[damage])
    elif damage == "later-denial":
        events.insert(
            2,
            _event(
                1,
                EventKind.APPROVAL_RECORDED,
                {"group_id": "group", "tool_call_ids": ["action"], "decision": "denied"},
            ).model_copy(update={"event_id": "denial"}),
        )
    elif damage in {"denied", "wrong-group", "multiple-actions"}:
        key, value = {
            "denied": ("decision", "denied"),
            "wrong-group": ("group_id", "other"),
            "multiple-actions": ("tool_call_ids", ["action", "other"]),
        }[damage]
        events[1] = events[1].model_copy(update={"payload": {**events[1].payload, key: value}})
    elif damage in {
        "wrong-action",
        "wrong-tool",
        "failed",
        "boolean-exit",
        "wrong-directory",
        "unknown-directory",
    }:
        key, value = {
            "wrong-action": ("tool_call_id", "other"),
            "wrong-tool": ("tool_name", "file_editor"),
            "failed": ("exit_code", 1),
            "boolean-exit": ("exit_code", False),
            "wrong-directory": ("working_directory", "/another"),
            "unknown-directory": ("working_directory", None),
        }[damage]
        events[2] = events[2].model_copy(update={"payload": {**events[2].payload, key: value}})
    elif damage == "wrong-session":
        events[3] = events[3].model_copy(update={"session_id": "other"})
    elif damage == "wrong-order":
        events[2] = events[2].model_copy(update={"sequence": 4})
    elif damage == "duplicate-event":
        events.append(events[3])
    elif damage == "duplicate-preparation":
        events.insert(1, _record(4, _prepared()))
    elif damage == "duplicate-observation":
        record = JournaledReproduction.model_validate(events[3].payload["observation"])
        events.append(_record(4, record))
    else:
        record = JournaledReproduction.model_validate(events[3].payload["observation"])
        if damage == "changed-root":
            record = record.model_copy(update={"project_directory": "/another"})
        elif damage == "changed-spec":
            record = record.model_copy(
                update={
                    "witness": record.witness.model_copy(
                        update={
                            "spec": record.witness.spec.model_copy(update={"directory": "other"})
                        }
                    )
                }
            )
        else:
            protected = record.witness.protected
            record = record.model_copy(
                update={
                    "witness": record.witness.model_copy(
                        update={
                            "protected": (
                                protected[0].model_copy(update={"sha256": "b" * 64}),
                                *protected[1:],
                            )
                        }
                    )
                }
            )
        events[3] = _record(3, record)
    with pytest.raises(ValueError, match=r"[Rr]eproduction"):
        reproduction_records(events, run_id="run", stage_id="verify")


@pytest.mark.parametrize("missing", ["preparation_event_id", "execution_event_id"])
def test_observation_requires_both_source_links(missing: str) -> None:
    value = JournaledReproduction.model_validate(_events()[3].payload["observation"]).model_dump()
    value[missing] = None
    with pytest.raises(ValidationError):
        JournaledReproduction.model_validate(value)


def test_live_observation_is_one_shot_and_late_files_do_not_repair_it() -> None:
    events = _events()[:3]
    inspector = Inspector()
    inspector.files.pop("reproduced/metrics.json")
    observed = observe_reproduction(_run(), events=events, execution=events[2], inspector=inspector)
    assert observed is not None
    assert observed.witness.status == "failed"
    events.append(_record(3, observed))
    inspector.files.update(_OUTPUTS)
    assert (
        observe_reproduction(_run(), events=events, execution=events[2], inspector=inspector)
        is None
    )
    assert (
        reproduction_records(events, run_id="run", stage_id="verify")[0][1].witness.status
        == "failed"
    )


def test_preparation_without_observation_remains_unverified() -> None:
    records = reproduction_records(_events()[:3], run_id="run", stage_id="verify")
    assert records[0][1].witness.status == "prepared"
    assert not records[0][1].witness.verifies(protected=_FILES, outputs=_OUTPUTS)


def test_ended_workflow_does_not_capture_a_late_tool_result() -> None:
    events = _events()[:3]
    assert (
        observe_reproduction(
            _run().model_copy(update={"phase": "cancelled"}),
            events=events,
            execution=events[2],
            inspector=Inspector(),
        )
        is None
    )
