# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Append-only local scientific records using Heartwood's recoverable JSONL writer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError

from heartwood.persistence import DurableFileError, LockedJsonlStore
from heartwood.schemas.experiments import (
    Digest,
    ExperimentEvent,
    ExperimentRecord,
    ExperimentRun,
    reduce_experiment_events,
)


class ExperimentSink(Protocol):
    """Append idempotent events and return an ordered, verified snapshot.

    Implementations must reject changed retries and invalid transitions atomically.
    The local implementation does not provide deployment-owned immutable retention.
    """

    def append(self, event: ExperimentEvent) -> None:
        """Persist exactly once or reject a conflicting identity or invalid history."""

    def events(self) -> tuple[ExperimentEvent, ...]:
        """Read authoritative records; never execute work during recovery."""


class _Entry(ExperimentRecord):
    schema_version: Literal["heartwood.experiment-entry.v1"] = "heartwood.experiment-entry.v1"
    sequence: int = Field(ge=1, strict=True)
    previous_sha256: Digest | None
    event: ExperimentEvent
    sha256: Digest


class _RecordedError(Exception):
    """Stop an exact retry before the generic appender writes a second line."""


class ExperimentStore:
    """Owner-private local journal, not a sandbox, authentication service, or attestation."""

    def __init__(self, path: Path) -> None:
        """Use a dedicated file within an initialized project's private state."""
        self._journal = LockedJsonlStore(path)

    def append(self, event: ExperimentEvent) -> None:
        """Validate under the shared append lock and preserve exact retry identity."""
        # Also validate model_construct/model_copy callers at the persistence boundary.
        event = ExperimentEvent.model_validate(event)

        def build(records: tuple[dict[str, Any], ...]) -> dict[str, Any]:
            entries = _validate_entries(records)
            events = tuple(entry.event for entry in entries)
            for recorded in events:
                if recorded.event_id == event.event_id:
                    if recorded != event:
                        raise ValueError("Experiment retry changed its original content")
                    raise _RecordedError
            reduce_experiment_events((*events, event))
            payload = {
                "schema_version": "heartwood.experiment-entry.v1",
                "sequence": len(entries) + 1,
                "previous_sha256": entries[-1].sha256 if entries else None,
                "event": event.model_dump(mode="json"),
            }
            return {**payload, "sha256": _digest(payload)}

        try:
            self._journal.append_derived(build)
        except _RecordedError:
            return
        except DurableFileError:
            raise ValueError("Experiment journal requires integrity recovery") from None

    def synchronize(self, events: Iterable[ExperimentEvent]) -> None:
        """Materialize source records without re-appending each already verified event."""
        recorded = {event.event_id: event for event in self.events()}
        for event in events:
            event = ExperimentEvent.model_validate(event)
            existing = recorded.get(event.event_id)
            if existing is not None:
                if existing != event:
                    raise ValueError("Experiment retry changed its original content")
                continue
            # The append lock still checks retries and transitions against concurrent writers.
            self.append(event)
            recorded[event.event_id] = event

    def events(self) -> tuple[ExperimentEvent, ...]:
        """Recover interrupted appends and verify the full available chain."""
        try:
            return tuple(entry.event for entry in _validate_entries(self._journal.read_objects()))
        except DurableFileError:
            raise ValueError("Experiment journal requires integrity recovery") from None

    def runs(self) -> tuple[ExperimentRun, ...]:
        """Return the shared run projection without model, tool, or file execution."""
        return reduce_experiment_events(self.events())

    def producers(self, *, path: str, sha256: str) -> tuple[ExperimentRun, ...]:
        """Find recorded producers of an exact output identity, including failed runs."""
        return tuple(
            run
            for run in self.runs()
            if any(output.path == path and output.sha256 == sha256 for output in run.outputs)
        )

    def export(self) -> bytes:
        """Return deterministic verified JSONL; signing and retention are separate controls."""
        try:
            entries = _validate_entries(self._journal.read_objects())
        except DurableFileError:
            raise ValueError("Experiment journal requires integrity recovery") from None
        return b"".join(_canonical(entry.model_dump(mode="json")) + b"\n" for entry in entries)


def _validate_entries(records: tuple[dict[str, Any], ...]) -> tuple[_Entry, ...]:
    try:
        entries = tuple(_Entry.model_validate(record) for record in records)
    except ValidationError:
        raise ValueError("Persisted experiment record is invalid") from None
    previous: str | None = None
    for sequence, entry in enumerate(entries, 1):
        if (
            entry.sequence != sequence
            or entry.previous_sha256 != previous
            or entry.sha256 != _digest(entry.model_dump(mode="json", exclude={"sha256"}))
        ):
            raise ValueError("Experiment journal integrity verification failed")
        previous = entry.sha256
    reduce_experiment_events(tuple(entry.event for entry in entries))
    return entries


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()
