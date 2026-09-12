# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Portable Python version metadata, not executable or environment attestation."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePath
from typing import Annotated, Literal, Self

from packaging.utils import canonicalize_name
from packaging.version import Version
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _package_name(value: str) -> str:
    canonicalize_name(value, validate=True)
    return value


def _version(value: str) -> str:
    Version(value)
    return value


def _absolute_python(value: str) -> str:
    if not PurePath(value).is_absolute():
        raise ValueError("Python execution requires an absolute interpreter path")
    return value


type MetadataText = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
]
type PackageName = Annotated[MetadataText, AfterValidator(_package_name)]
type PackageVersion = Annotated[MetadataText, AfterValidator(_version)]
type PythonExecutable = Annotated[
    str,
    Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f\x7f]+$"),
    AfterValidator(_absolute_python),
]


class PythonEnvironmentSnapshot(BaseModel):
    """Interpreter identity and declared installed versions without paths or secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["heartwood.python-environment.v1"] = "heartwood.python-environment.v1"
    implementation: MetadataText
    python: PackageVersion
    system: MetadataText
    machine: MetadataText
    packages: tuple[tuple[PackageName, PackageVersion], ...] = Field(max_length=10000)

    @model_validator(mode="after")
    def distinct_distributions(self) -> Self:
        """Reject ambiguous duplicate distributions, including alternate name spellings."""
        names = [canonicalize_name(name) for name, _ in self.packages]
        if len(names) != len(set(names)):
            raise ValueError("Python environment contains duplicate package identities")
        return self

    @property
    def fingerprint(self) -> str:
        """Preserve the experiment recorder's existing metadata digest representation."""
        descriptor = self.model_dump(exclude={"schema_version"})
        descriptor["packages"] = sorted(self.packages)
        return hashlib.sha256(
            json.dumps(
                descriptor,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()

    def differences(self, observed: PythonEnvironmentSnapshot) -> tuple[str, ...]:
        """Compare platform and required versions; additional packages are not verified."""
        changes = [
            name
            for name in ("implementation", "system", "machine")
            if getattr(self, name) != getattr(observed, name)
        ]
        if Version(self.python) != Version(observed.python):
            changes.append("python")
        actual = {canonicalize_name(name): Version(version) for name, version in observed.packages}
        changes.extend(
            f"package:{canonicalize_name(name)}"
            for name, version in self.packages
            if actual.get(canonicalize_name(name)) != Version(version)
        )
        return tuple(sorted(changes))
