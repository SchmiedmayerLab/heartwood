# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Language-neutral scientific execution records, separate from the security audit."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from heartwood.schemas.project_paths import project_relative_path

type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


def _public_path(value: str) -> str:
    return project_relative_path(value, allow_root=False).as_posix()


type ExperimentPath = Annotated[
    str, Field(min_length=1, max_length=512), AfterValidator(_public_path)
]


class ExperimentRecord(BaseModel):
    """Closed records whose JSON Schema can be used by any language binding."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        revalidate_instances="always",
        json_schema_serialization_defaults_required=True,
    )


class ExperimentFile(ExperimentRecord):
    """A declared project file observed at a boundary, without its contents."""

    path: ExperimentPath
    sha256: Digest
    size_bytes: int = Field(ge=0, strict=True)


class ExperimentEnvironment(ExperimentRecord):
    """Fingerprint of an environment description, not an environment attestation."""

    kind: Literal["python", "container", "declared"]
    sha256: Digest
    source: Literal["observed", "declared"]

    @model_validator(mode="after")
    def _declared_source(self) -> Self:
        if self.kind == "declared" and self.source != "declared":
            raise ValueError("A declared environment cannot claim observed evidence")
        return self


class ExperimentStage(ExperimentRecord):
    """Association with the owning Heartwood session and research stage."""

    session_id: Reference
    workflow_run_id: Reference
    stage_id: Reference
    tool_call_id: Reference | None = None


class ExperimentDefinition(ExperimentRecord):
    """Immutable execution inputs; retries cannot silently change an experiment."""

    actor_ref: Reference
    source: Literal["python", "shell", "heartwood"]
    entry_point: ExperimentPath
    code: tuple[ExperimentFile, ...] = Field(min_length=1, max_length=256)
    inputs: tuple[ExperimentFile, ...] = Field(default=(), max_length=256)
    output_paths: tuple[ExperimentPath, ...] = Field(default=(), max_length=256)
    environment: ExperimentEnvironment
    parameters_sha256: Digest
    invocation_sha256: Digest
    git_revision: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")] | None = None
    git_dirty: bool | None = Field(default=None, strict=True)
    stage: ExperimentStage | None = None

    @model_validator(mode="after")
    def _identities(self) -> Self:
        code_paths = tuple(item.path for item in self.code)
        input_paths = tuple(item.path for item in self.inputs)
        for paths in (code_paths, input_paths, self.output_paths):
            if len(paths) != len({path.casefold() for path in paths}):
                raise ValueError("Experiment file references must be unique")
        if self.entry_point not in code_paths:
            raise ValueError("The executable entry point must have a code fingerprint")
        references: dict[str, ExperimentFile] = {}
        for item in (*self.code, *self.inputs):
            key = item.path.casefold()
            if key in references and references[key] != item:
                raise ValueError("A shared experiment file must have one unambiguous fingerprint")
            references[key] = item
        protected = {path.casefold() for path in (*code_paths, *input_paths)}
        output_paths = {path.casefold() for path in self.output_paths}
        if any(
            output == path or output.startswith(path + "/") or path.startswith(output + "/")
            for output in output_paths
            for path in protected | (output_paths - {output})
        ):
            raise ValueError("Experiment outputs must not overwrite declared inputs or code")
        if self.source == "heartwood" and self.stage is None:
            raise ValueError("Heartwood experiments require their owning workflow stage")
        if self.git_revision is None and self.git_dirty is not None:
            raise ValueError("Git cleanliness requires a known revision")
        return self


type ExperimentStatus = Literal[
    "started", "interrupted", "resumed", "succeeded", "failed", "cancelled"
]


class ExperimentEvent(ExperimentRecord):
    """One idempotent append; timestamps and identities come from the record owner."""

    schema_version: Literal["heartwood.experiment-event.v1"] = "heartwood.experiment-event.v1"
    event_id: UUID
    run_id: UUID
    status: ExperimentStatus
    at: AwareDatetime
    attempt: int = Field(ge=1, strict=True)
    definition: ExperimentDefinition | None = None
    outputs: tuple[ExperimentFile, ...] = Field(default=(), max_length=256)
    exit_code: int | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def _payload(self) -> Self:
        if (self.status == "started") != (self.definition is not None):
            raise ValueError("Only the first event declares the experiment definition")
        if self.status == "started" and self.attempt != 1:
            raise ValueError("An experiment must start at attempt one")
        if self.status not in {"succeeded", "failed", "cancelled"} and (
            self.outputs or self.exit_code is not None
        ):
            raise ValueError("Only terminal outcomes can contain output observations")
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("Successful execution requires a zero exit code")
        if self.status == "failed" and self.exit_code == 0:
            raise ValueError("Failed execution cannot have a zero exit code")
        paths = tuple(item.path.casefold() for item in self.outputs)
        if len(paths) != len(set(paths)):
            raise ValueError("Output observations must be unique")
        return self


class ExperimentRun(ExperimentRecord):
    """One shared projection derived from the scientific execution journal."""

    schema_version: Literal["heartwood.experiment-run.v1"] = "heartwood.experiment-run.v1"
    run_id: UUID
    definition: ExperimentDefinition
    started_at: AwareDatetime
    updated_at: AwareDatetime
    status: ExperimentStatus
    attempt: int = Field(ge=1, strict=True)
    outputs: tuple[ExperimentFile, ...] = ()
    exit_code: int | None = Field(default=None, strict=True)


def reduce_experiment_events(events: tuple[ExperimentEvent, ...]) -> tuple[ExperimentRun, ...]:
    """Validate ordered transitions and derive runs without executing or resuming work."""
    runs: dict[UUID, ExperimentRun] = {}
    identities: set[UUID] = set()
    for event in events:
        if event.event_id in identities:
            raise ValueError("Duplicate experiment event identity")
        identities.add(event.event_id)
        previous = runs.get(event.run_id)
        if event.status == "started":
            if previous is not None or event.definition is None:
                raise ValueError("An experiment identity cannot be started again")
            definition = event.definition
            started_at = event.at
        else:
            if previous is None:
                raise ValueError("Experiment outcome has no start record")
            if previous.status in {"succeeded", "failed", "cancelled"}:
                raise ValueError("A terminal experiment outcome cannot be replaced")
            if event.at < previous.updated_at:
                raise ValueError("Experiment time cannot move backwards")
            if event.status == "resumed":
                if previous.status != "interrupted" or event.attempt != previous.attempt + 1:
                    raise ValueError("Resume requires an interrupted preceding attempt")
            elif (event.attempt != previous.attempt or previous.status == "interrupted") and not (
                previous.status == "interrupted"
                and event.status == "cancelled"
                and event.attempt == previous.attempt
            ):
                raise ValueError("Experiment transition does not match the active attempt")
            definition = previous.definition
            started_at = previous.started_at
        declared = set(definition.output_paths)
        observed = {item.path for item in event.outputs}
        if not observed.issubset(declared):
            raise ValueError("Experiment outcome contains undeclared outputs")
        if event.status == "succeeded" and observed != declared:
            raise ValueError("Successful execution requires every declared output")
        runs[event.run_id] = ExperimentRun(
            run_id=event.run_id,
            definition=definition,
            started_at=started_at,
            updated_at=event.at,
            status=event.status,
            attempt=event.attempt,
            outputs=event.outputs,
            exit_code=event.exit_code,
        )
    return tuple(runs.values())
