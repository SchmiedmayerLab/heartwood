# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Project-local Python experiment recording without a second execution engine."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import distributions
from uuid import UUID, uuid4

from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._workspace import WorkspaceInspector
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
        identity = UUID(str(run_id)) if run_id is not None else uuid4()
        self.project.initialize()
        with native_file_lock(self.project.state_root / f".experiment-{identity}.lock", timeout=0):
            if any(run.run_id == identity for run in self._store.runs()):
                raise ValueError("Experiment identity already exists; inspect its recorded outcome")
            self._verify_inputs(definition)
            if any(not self._workspace.is_absent(path) for path in definition.output_paths):
                raise ValueError("Experiment outputs require unused paths in existing directories")
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
            try:
                yield identity
                self._verify_inputs(definition)
                outputs = tuple(self._file(path) for path in definition.output_paths)
                self._verify_inputs(definition)
            except BaseException as error:
                self._store.append(
                    ExperimentEvent(
                        event_id=uuid4(),
                        run_id=identity,
                        at=datetime.now(UTC),
                        attempt=1,
                        status="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
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
                        attempt=1,
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
    packages = sorted((item.metadata["Name"], item.version) for item in distributions())
    return ExperimentEnvironment(
        kind="python",
        source="observed",
        sha256=experiment_digest(
            {
                "implementation": platform.python_implementation(),
                "python": platform.python_version(),
                "system": platform.system(),
                "machine": platform.machine(),
                "packages": packages,
            }
        ),
    )
