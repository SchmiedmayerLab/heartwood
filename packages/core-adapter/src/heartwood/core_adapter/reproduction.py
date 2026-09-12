# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-minimized evidence for an observed, separately reviewed reproduction."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood.schemas.project_paths import project_relative_path
from heartwood.schemas.python_environment import PythonExecutable


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReproductionFile(_Record):
    """One inspected project file, without retaining its contents."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        """Reject private, ambiguous, and out-of-project paths."""
        project_relative_path(self.path, allow_root=False)
        return self


class ReproductionSpec(_Record):
    """Exact program invocation and files whose observed identity must be preserved."""

    program: str
    data: str
    purpose: Literal["analysis", "python-environment"] = "analysis"
    python_executable: PythonExecutable | None = None
    required_environment: str | None = None
    analysis_environment: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lockfile: str | None = None
    directory: str
    protected_paths: tuple[str, ...] = Field(min_length=2, max_length=128)
    output_names: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        """Keep outputs distinct from inputs and disallow overlapping file identities."""
        if self.analysis_environment is not None and self.python_executable is None:
            raise ValueError("Environment reconstruction requires its control Python")
        if self.lockfile is not None and (
            self.analysis_environment is None
            or self.purpose != "python-environment"
            or self.lockfile not in self.protected_paths
        ):
            raise ValueError("Reconstruction requires a protected dependency lock")
        if self.analysis_environment is not None and (
            (self.purpose == "python-environment" and self.lockfile is None)
            or (self.purpose == "analysis" and self.required_environment is None)
        ):
            raise ValueError("Reconstruction requires a lock or an observed environment guard")
        if self.purpose == "python-environment" and (
            self.python_executable is None or self.output_names != ("environment.json",)
        ):
            raise ValueError("Environment capture requires a bound Python and its fixed output")
        if self.required_environment is not None and (
            self.purpose != "analysis"
            or self.python_executable is None
            or self.required_environment not in self.protected_paths
        ):
            raise ValueError("An environment guard requires bound Python and a protected record")
        for path in (self.program, self.data, self.directory, *self.output_names):
            project_relative_path(path, allow_root=False)
        if any(path.startswith("-") for path in (self.program, self.data, self.directory)):
            raise ValueError("Reproduction arguments must be paths, not command options")
        if self.program == self.data or not {self.program, self.data}.issubset(
            self.protected_paths
        ):
            raise ValueError("Reproduction must preserve its distinct program and data files")
        paths = (*self.protected_paths, *self.output_paths)
        normalized = [project_relative_path(path, allow_root=False) for path in paths]
        folded = [PurePosixPath(str(path).casefold()) for path in normalized]
        for index, normalized_path in enumerate(folded):
            if any(
                normalized_path == other
                or normalized_path in other.parents
                or other in normalized_path.parents
                for other in folded[index + 1 :]
            ):
                raise ValueError("Reproduction file paths must be distinct and non-overlapping")
        directory = PurePosixPath(self.directory.casefold())
        for path in self.protected_paths:
            protected = PurePosixPath(path.casefold())
            if (
                directory == protected
                or directory in protected.parents
                or protected in directory.parents
            ):
                raise ValueError("Reproduction destination must not overlap protected files")
        return self

    @property
    def command(self) -> str:
        """Return a shell-quoted invocation without allowing model-supplied shell syntax."""
        if self.analysis_environment is not None:
            assert self.python_executable is not None
            arguments = (
                ("--lockfile", self.lockfile, "--expected", self.data)
                if self.purpose == "python-environment"
                else (
                    "--require",
                    self.required_environment,
                    "--program",
                    self.program,
                    "--data",
                    self.data,
                )
            )
            assert all(isinstance(value, str) for value in arguments)
            return shlex.join(
                (
                    self.python_executable,
                    "-I",
                    "-m",
                    "heartwood.gateway._environment_probe",
                    "--environment-id",
                    self.analysis_environment,
                    "--output-dir",
                    self.directory,
                    *(str(value) for value in arguments),
                )
            )
        if self.purpose == "python-environment":
            assert self.python_executable is not None
            return shlex.join(
                (
                    self.python_executable,
                    "-I",
                    "-m",
                    "heartwood.gateway._environment_probe",
                    "--output-dir",
                    self.directory,
                )
            )
        interpreter = (self.python_executable, "-I") if self.python_executable else ("python",)
        command = shlex.join(
            (*interpreter, self.program, "--data", self.data, "--output-dir", self.directory)
        )
        if self.required_environment is not None:
            guard = shlex.join(
                (
                    *interpreter,
                    "-m",
                    "heartwood.gateway._environment_probe",
                    "--require",
                    self.required_environment,
                )
            )
            return f"{guard} && {command}"
        return command

    @property
    def output_paths(self) -> tuple[str, ...]:
        """Return project-relative output paths under the dedicated destination."""
        return tuple(str(PurePosixPath(self.directory) / name) for name in self.output_names)

    def matches_command(self, command: str) -> bool:
        """Accept only the published invocation, not equivalent-looking shell programs."""
        return command == self.command


class ReproductionWitness(_Record):
    """Owner-supplied boundary observations, not permission or an execution attestation.

    The caller must capture preparation before dispatch and observation before
    admitting subsequent actions. Deserialization cannot establish that history.
    The surrounding session journal supplies authority and execution ordering.
    """

    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    stage_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    spec: ReproductionSpec
    protected: tuple[ReproductionFile, ...]
    outputs: tuple[ReproductionFile, ...] = ()
    status: Literal["prepared", "succeeded", "failed"] = "prepared"

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        """Reject incomplete, duplicated, or conflicting recorded file sets."""
        if tuple(item.path for item in self.protected) != tuple(sorted(self.spec.protected_paths)):
            raise ValueError("Reproduction requires each protected file exactly once")
        expected = tuple(sorted(self.spec.output_paths)) if self.status == "succeeded" else ()
        if tuple(item.path for item in self.outputs) != expected:
            raise ValueError("Reproduction output evidence does not match its status")
        return self

    @classmethod
    def prepare(
        cls,
        *,
        session_id: str,
        run_id: str,
        stage_id: str,
        tool_call_id: str,
        spec: ReproductionSpec,
        command: str,
        group_size: int,
        destination_absent: bool,
        expected: Mapping[str, str],
        observed: Mapping[str, str],
    ) -> Self | None:
        """Bind a separate proposed execution to unchanged, completely inspected inputs."""
        if (
            group_size != 1
            or not destination_absent
            or not spec.matches_command(command)
            or set(expected) != set(spec.protected_paths)
            or expected != observed
        ):
            return None
        return cls(
            session_id=session_id,
            run_id=run_id,
            stage_id=stage_id,
            tool_call_id=tool_call_id,
            spec=spec,
            protected=_fingerprints(expected),
        )

    def observe(
        self,
        *,
        tool_call_id: str,
        approved: bool,
        exit_code: int,
        protected: Mapping[str, str],
        outputs: Mapping[str, str],
    ) -> Self:
        """Capture the first settled outcome; failed or missing proof cannot be repaired."""
        if self.status != "prepared":
            return self
        succeeded = (
            tool_call_id == self.tool_call_id
            and approved
            and exit_code == 0
            and _fingerprints(protected) == self.protected
            and set(outputs) == set(self.spec.output_paths)
        )
        return self.model_copy(
            update={
                "status": "succeeded" if succeeded else "failed",
                "outputs": _fingerprints(outputs) if succeeded else (),
            }
        )

    def verifies(self, *, protected: Mapping[str, str], outputs: Mapping[str, str]) -> bool:
        """Require complete, unchanged observed files without reading or executing them."""
        return (
            self.status == "succeeded"
            and _fingerprints(protected) == self.protected
            and _fingerprints(outputs) == self.outputs
        )


def _fingerprints(files: Mapping[str, str]) -> tuple[ReproductionFile, ...]:
    return tuple(
        ReproductionFile(path=path, sha256=hashlib.sha256(content.encode("utf-8")).hexdigest())
        for path, content in sorted(files.items())
    )
