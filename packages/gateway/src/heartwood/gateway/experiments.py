# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project-local Python experiment recording without a second execution engine."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._workspace import WorkspaceInspector
from heartwood.gateway.python_environment import observe_python_environment
from heartwood.persistence import native_file_lock
from heartwood.schemas.experiments import (
    ExperimentDefinition,
    ExperimentEnvironment,
    ExperimentEvent,
    ExperimentFile,
    ExperimentRun,
)


class ExperimentRecorder:
    """Record declared project code and inputs around an ordinary Python code block.

    Recording is not a sandbox or scientific validation. It captures boundary
    observations and cannot identify undeclared dependencies or hidden side effects.
    """

    def __init__(self, project: ProjectContext, *, max_file_bytes: int = 1024**3) -> None:
        """Bind to one project and an explicit per-file fingerprint budget."""
        if type(max_file_bytes) is not int or max_file_bytes < 1:
            raise ValueError("Experiment file budget must be positive")
        self.project = project
        self.max_file_bytes = max_file_bytes
        self._workspace = WorkspaceInspector(project)
        self._store = ExperimentStore(project.state_root / "experiments.jsonl")

    def describe(
        self,
        *,
        actor_ref: str,
        entry_point: str,
        inputs: tuple[str, ...],
        outputs: tuple[str, ...],
        parameters: Mapping[str, object],
        code: tuple[str, ...] = (),
        environment: ExperimentEnvironment | None = None,
    ) -> ExperimentDefinition:
        """Fingerprint declared files and parameters without initializing state or running code."""
        code_paths = tuple(dict.fromkeys((entry_point, *code)))
        return ExperimentDefinition(
            actor_ref=actor_ref,
            source="python",
            entry_point=entry_point,
            code=tuple(self._file(path) for path in code_paths),
            inputs=tuple(self._file(path) for path in inputs),
            output_paths=outputs,
            environment=environment or observed_python_environment(),
            parameters_sha256=experiment_digest(dict(parameters)),
            invocation_sha256=experiment_digest({"entry_point": entry_point, "binding": "python"}),
        )

    @contextmanager
    def record(
        self, definition: ExperimentDefinition, *, run_id: UUID | None = None
    ) -> Iterator[UUID]:
        """Persist a start before entering user code and observe its terminal outcome.

        Reusing a run identity never reenters the body. An abrupt process loss
        leaves an unfinished record for explicit recovery rather than rerunning it.
        Exceptions are propagated without copying their text into the journal.
        """
        definition = ExperimentDefinition.model_validate(definition)
        self._require_script(definition)
        identity = UUID(str(run_id)) if run_id is not None else uuid4()
        self.project.initialize()
        with native_file_lock(
            self.project.state_root / f".experiment-{identity}.lock", timeout=0, reentrant=False
        ):
            if any(run.run_id == identity for run in self._store.runs()):
                raise ValueError("Experiment identity already exists; inspect its recorded outcome")
            self._verify_preconditions(definition)
            self._store.append(
                ExperimentEvent(
                    event_id=uuid4(),
                    run_id=identity,
                    status="started",
                    at=datetime.now(UTC),
                    attempt=1,
                    definition=definition,
                )
            )
            with self._observe_outcome(definition, identity=identity, attempt=1):
                yield identity

    def recover(self, run_id: UUID) -> ExperimentRun:
        """Mark an abandoned script attempt interrupted without inferring its effects.

        The run's native lease must be free. This does not stop detached children,
        undo outputs, certify safe retry, or reenter caller code.
        """
        with self._existing_run(run_id) as run:
            if run.status == "interrupted":
                return run
            if run.status not in {"started", "resumed"}:
                raise ValueError("The experiment already has a terminal outcome")
            self._store.append(
                ExperimentEvent(
                    event_id=uuid4(),
                    run_id=run.run_id,
                    at=datetime.now(UTC),
                    attempt=run.attempt,
                    status="interrupted",
                )
            )
            return self._find_run(run.run_id)

    def record_command(
        self,
        *,
        actor_ref: str,
        entry_point: str,
        inputs: tuple[str, ...] = (),
        outputs: tuple[str, ...] = (),
        arguments: tuple[str, ...] = (),
        code: tuple[str, ...] = (),
        runner: str | None = None,
        environment: ExperimentEnvironment | None = None,
        run_id: UUID | None = None,
    ) -> ExperimentRun:
        """Record a user-requested script through the same Python recording boundary.

        The default interpreter is Heartwood's Python. Other interpreters require
        an explicit environment declaration. Arguments are passed literally;
        shell expansion is not performed. Output goes to the caller's terminal,
        never the scientific journal. This is not an agent tool or a sandbox.
        """
        if runner is not None and (environment is None or environment.source != "declared"):
            raise ValueError("Another interpreter requires a declared environment fingerprint")
        executable = sys.executable if runner is None else shutil.which(runner)
        if executable is None:
            raise ValueError("The requested interpreter is unavailable")
        definition = self.describe(
            actor_ref=actor_ref,
            entry_point=entry_point,
            inputs=inputs,
            outputs=outputs,
            parameters={"arguments": arguments},
            code=code,
            environment=environment,
        )
        definition = ExperimentDefinition.model_validate(
            {
                **definition.model_dump(),
                "source": "shell",
                "invocation_sha256": experiment_digest(
                    {"runner": executable, "entry_point": entry_point, "arguments": arguments}
                ),
            }
        )
        with self.record(definition, run_id=run_id) as identity:
            subprocess.run(
                (executable, str(self.project.root / entry_point), *arguments),
                cwd=self.project.root,
                check=True,
            )
        return self._find_run(identity)

    def cancel(self, run_id: UUID) -> ExperimentRun:
        """Close an abandoned attempt without deleting files or claiming rollback."""
        with self._existing_run(run_id) as run:
            if run.status == "cancelled":
                return run
            if run.status not in {"started", "resumed", "interrupted"}:
                raise ValueError("The experiment already has a terminal outcome")
            self._store.append(
                ExperimentEvent(
                    event_id=uuid4(),
                    run_id=run.run_id,
                    at=datetime.now(UTC),
                    attempt=run.attempt,
                    status="cancelled",
                )
            )
            return self._find_run(run.run_id)

    @contextmanager
    def resume(self, run_id: UUID) -> Iterator[UUID]:
        """Explicitly retry an interrupted script with its unchanged declaration.

        The caller must verify that repeating external effects is safe. Declared
        outputs must be absent; this method never removes partial results.
        A resumed intent is durable before caller code is entered.
        """
        with self._existing_run(run_id) as run:
            if run.status != "interrupted":
                raise ValueError("Resume requires explicit recovery of an interrupted experiment")
            self._verify_preconditions(run.definition)
            attempt = run.attempt + 1
            self._store.append(
                ExperimentEvent(
                    event_id=uuid4(),
                    run_id=run.run_id,
                    at=datetime.now(UTC),
                    attempt=attempt,
                    status="resumed",
                )
            )
            with self._observe_outcome(run.definition, identity=run.run_id, attempt=attempt):
                yield run.run_id

    @contextmanager
    def _existing_run(self, run_id: UUID) -> Iterator[ExperimentRun]:
        identity = UUID(str(run_id))
        if not self.project.state_exists():
            raise ValueError("No experiment exists in this project")
        with native_file_lock(
            self.project.state_root / f".experiment-{identity}.lock", timeout=0, reentrant=False
        ):
            run = self._find_run(identity)
            self._require_script(run.definition)
            yield run

    def _find_run(self, identity: UUID) -> ExperimentRun:
        run = next((item for item in self._store.runs() if item.run_id == identity), None)
        if run is None:
            raise ValueError("No experiment has this identity")
        return run

    @staticmethod
    def _require_script(definition: ExperimentDefinition) -> None:
        if definition.source == "heartwood":
            raise ValueError("Workflow experiment records are owned by the session gateway")

    def _verify_preconditions(self, definition: ExperimentDefinition) -> None:
        self._verify_inputs(definition)
        if any(not self._workspace.is_absent(path) for path in definition.output_paths):
            raise ValueError("Experiment outputs require unused paths in existing directories")

    @contextmanager
    def _observe_outcome(
        self, definition: ExperimentDefinition, *, identity: UUID, attempt: int
    ) -> Iterator[None]:
        try:
            yield
            self._verify_inputs(definition)
            outputs = tuple(self._file(path) for path in definition.output_paths)
            self._verify_inputs(definition)
        except BaseException as error:
            self._store.append(
                ExperimentEvent(
                    event_id=uuid4(),
                    run_id=identity,
                    at=datetime.now(UTC),
                    attempt=attempt,
                    status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                    exit_code=(
                        error.returncode
                        if isinstance(error, subprocess.CalledProcessError)
                        else None
                    ),
                )
            )
            raise
        else:
            self._store.append(
                ExperimentEvent(
                    event_id=uuid4(),
                    run_id=identity,
                    status="succeeded",
                    at=datetime.now(UTC),
                    attempt=attempt,
                    outputs=outputs,
                    exit_code=0,
                )
            )

    def runs(self) -> tuple[ExperimentRun, ...]:
        """Read the shared projection without creating an uninitialized project."""
        if not self.project.state_exists():
            return ()
        return self._store.runs()

    def export(self) -> bytes:
        """Export deterministic private records, not a signed or retained attestation."""
        if not self.project.state_exists():
            return b""
        return self._store.export()

    def _verify_inputs(self, definition: ExperimentDefinition) -> None:
        if (
            definition.environment.kind == "python"
            and definition.environment.source == "observed"
            and observed_python_environment() != definition.environment
        ):
            raise ValueError("The observed experiment environment changed")
        for expected in (*definition.code, *definition.inputs):
            if self._file(expected.path) != expected:
                raise ValueError("Declared experiment code or inputs changed")

    def _file(self, path: str) -> ExperimentFile:
        return self._workspace.fingerprint(path, max_bytes=self.max_file_bytes)


def experiment_digest(value: object) -> str:
    """Hash explicit JSON values; digests are not anonymization or secret protection."""
    try:
        content = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError):
        raise ValueError("Experiment metadata must contain finite JSON values") from None
    return hashlib.sha256(content.encode()).hexdigest()


def observed_python_environment() -> ExperimentEnvironment:
    """Hash interpreter and package versions without paths, URLs, or environment values."""
    return ExperimentEnvironment(
        kind="python",
        source="observed",
        sha256=observe_python_environment().fingerprint,
    )
