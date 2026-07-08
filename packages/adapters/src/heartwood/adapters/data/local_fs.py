# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Root-confined local filesystem data-source adapter."""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from heartwood.adapters import DatasetFingerprint
from heartwood.schemas import JsonValue

_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DataSourceBoundaryError(ValueError):
    """Raised when a local data request escapes the configured fixture root."""


class LocalFilesystemDataSourceAdapter:
    """Read bounded previews from a synthetic, root-confined CSV fixture tree."""

    def __init__(self, root: Path) -> None:
        """Initialize the adapter with a filesystem root boundary."""
        self.root = root.resolve()

    @classmethod
    def synthetic_omop(cls, repo_root: Path | None = None) -> LocalFilesystemDataSourceAdapter:
        """Return an adapter over the checked-in synthetic OMOP-like fixtures."""
        base = Path.cwd() if repo_root is None else repo_root
        return cls(base / "fixtures" / "synthetic" / "omop-like")

    @property
    def source_id(self) -> str:
        """Return the stable data-source id."""
        return "local-fs"

    def fingerprint(self) -> DatasetFingerprint:
        """Return an OMOP-like fingerprint using filenames and headers only."""
        evidence: list[str] = []
        confidence = 0.0
        for table, expected_columns in {
            "person": {"person_id", "gender_concept_id", "year_of_birth"},
            "condition_occurrence": {
                "condition_occurrence_id",
                "person_id",
                "condition_concept_id",
            },
        }.items():
            try:
                columns = set(self.table_columns(table))
            except DataSourceBoundaryError:
                continue
            if expected_columns.issubset(columns):
                evidence.append(f"found {table}.csv with expected headers")
                confidence += 0.45
        if len(evidence) == 2:
            confidence = 0.95
        return DatasetFingerprint(
            dataset_type="omop-cdm",
            confidence=confidence,
            evidence=tuple(evidence) if evidence else ("no OMOP-like CSV headers detected",),
        )

    def table_columns(self, name: str) -> tuple[str, ...]:
        """Return table columns without reading row values."""
        path = self._table_path(name)
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.reader(file)
            return tuple(next(reader, ()))

    def read_table(
        self,
        name: str,
        columns: Sequence[str] | None = None,
        limit: int = 20,
    ) -> Sequence[Mapping[str, JsonValue]]:
        """Read a bounded table preview under the configured root."""
        if limit < 0:
            msg = "limit must be non-negative"
            raise DataSourceBoundaryError(msg)
        path = self._table_path(name)
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            selected = tuple(reader.fieldnames or ()) if columns is None else tuple(columns)
            rows: list[Mapping[str, JsonValue]] = []
            for index, row in enumerate(reader):
                if index >= limit:
                    break
                rows.append({key: row[key] for key in selected if key in row})
            return rows

    def _table_path(self, name: str) -> Path:
        if not _TABLE_NAME.fullmatch(name):
            msg = f"invalid table name: {name}"
            raise DataSourceBoundaryError(msg)
        path = (self.root / f"{name}.csv").resolve()
        if path != self.root and self.root not in path.parents:
            msg = f"table path escapes fixture root: {name}"
            raise DataSourceBoundaryError(msg)
        if not path.is_file():
            msg = f"table does not exist: {name}"
            raise DataSourceBoundaryError(msg)
        return path
