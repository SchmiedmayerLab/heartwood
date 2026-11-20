# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The project data catalog: the datasets of each project and their objects.

Each object has a DRS identifier, ``<dataset>.<name>``, a size and a SHA-256 checksum. Its
bytes live in the project's bucket under ``datasets/<dataset>/<name>``, and a project data
volume holds it at ``<dataset>/<name>``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

ProjectId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")]
DatasetId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]
# A file name without a path, which is also part of a DRS identifier.
ObjectName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$", max_length=128),
]
Text = Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r"\S")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogObject(_Model):
    """One file of a dataset."""

    name: ObjectName
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = Field(pattern=r"^[a-z]+/[a-z0-9.+-]+$", max_length=128)


class Dataset(_Model):
    """A dataset of a project: its description and its objects."""

    id: DatasetId
    name: Text
    description: Text
    created: AwareDatetime
    objects: tuple[CatalogObject, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [item.name for item in self.objects]
        if len(set(names)) != len(names):
            raise ValueError("object names of a dataset must be unique")
        return self


class CatalogProject(_Model):
    """A project, its members and its datasets."""

    id: ProjectId
    members: frozenset[str] = frozenset()
    datasets: tuple[Dataset, ...] = ()


@dataclass(frozen=True)
class Entry:
    """An object of the catalog together with its dataset and project."""

    project: str
    dataset: Dataset
    item: CatalogObject

    @property
    def id(self) -> str:
        """The DRS identifier of the object."""
        return object_id(self.dataset.id, self.item.name)

    @property
    def path(self) -> str:
        """The path of the object in a project data volume."""
        return f"{self.dataset.id}/{self.item.name}"

    @property
    def bucket(self) -> str:
        """The bucket of the object's project."""
        return f"hw-project-{self.project}"

    @property
    def key(self) -> str:
        """The key of the object in its project's bucket."""
        return f"datasets/{self.dataset.id}/{self.item.name}"


def object_id(dataset: str, name: str) -> str:
    """Return the DRS identifier of an object of a dataset."""
    return f"{dataset}.{name}"


def content_sha256_of(objects: list[tuple[str, str, int, str]]) -> str:
    """Identify volume content by each object's identifier, path, size and checksum."""
    manifest = sorted(
        (
            {"id": i, "path": path, "size": size, "sha256": sha256}
            for i, path, size, sha256 in objects
        ),
        key=lambda item: str(item["path"]),
    )
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def content_sha256(entries: list[Entry]) -> str:
    """Identify the content of a project data volume that holds ``entries``."""
    return content_sha256_of([(e.id, e.path, e.item.size, e.item.sha256) for e in entries])


class Catalog(_Model):
    """Every project of the platform with its datasets."""

    projects: tuple[CatalogProject, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        projects = [project.id for project in self.projects]
        if len(set(projects)) != len(projects):
            raise ValueError("project identifiers must be unique")
        # A dataset identifier names one dataset on the whole platform, so a DRS
        # identifier names one object.
        datasets = [dataset.id for project in self.projects for dataset in project.datasets]
        if len(set(datasets)) != len(datasets):
            raise ValueError("dataset identifiers must be unique across projects")
        return self

    def is_member(self, subject: str, project: str) -> bool:
        """Return whether a subject is a member of a project."""
        return any(p.id == project and subject in p.members for p in self.projects)

    def datasets(self, project: str) -> tuple[Dataset, ...]:
        """Return the datasets of a project."""
        for candidate in self.projects:
            if candidate.id == project:
                return candidate.datasets
        return ()

    def entries(self, project: str) -> list[Entry]:
        """Return every object of a project."""
        return [
            Entry(project, dataset, item)
            for dataset in self.datasets(project)
            for item in dataset.objects
        ]

    def entry(self, project: str, identifier: str) -> Entry | None:
        """Return an object of a project by its DRS identifier, or None.

        An object of another project is not returned, as if it did not exist.
        """
        for found in self.entries(project):
            if found.id == identifier:
                return found
        return None
