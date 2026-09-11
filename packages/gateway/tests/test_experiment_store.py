# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

import heartwood.persistence._files as files
from heartwood.gateway._experiment_store import ExperimentStore
from heartwood.persistence import EXPERIMENT_ENTRY_VERSION, EXPERIMENT_EVENT_VERSION
from heartwood.schemas.experiments import (
    ExperimentDefinition,
    ExperimentEnvironment,
    ExperimentEvent,
    ExperimentFile,
    reduce_experiment_events,
)

NOW = datetime(2026, 9, 11, tzinfo=UTC)
DIGEST = hashlib.sha256(b"synthetic").hexdigest()


def definition(**updates: object) -> ExperimentDefinition:
    values: dict[str, Any] = {
        "actor_ref": "synthetic-researcher",
        "source": "python",
        "entry_point": "analysis.py",
        "code": [ExperimentFile(path="analysis.py", sha256=DIGEST, size_bytes=9)],
        "inputs": [ExperimentFile(path="data.csv", sha256=DIGEST, size_bytes=9)],
        "output_paths": ["result.json"],
        "environment": ExperimentEnvironment(kind="python", sha256=DIGEST, source="observed"),
        "parameters_sha256": DIGEST,
        "invocation_sha256": DIGEST,
    }
    return ExperimentDefinition.model_validate({**values, **updates})


def start(**updates: object) -> ExperimentEvent:
    return ExperimentEvent.model_validate(
        {
            "event_id": uuid4(),
            "run_id": uuid4(),
            "at": NOW,
            "status": "started",
            "attempt": 1,
            "definition": definition(),
            **updates,
        }
    )


def after(first: ExperimentEvent, **updates: object) -> ExperimentEvent:
    return ExperimentEvent.model_validate(
        {
            "event_id": uuid4(),
            "run_id": first.run_id,
            "at": first.at + timedelta(seconds=1),
            "status": "succeeded",
            "attempt": first.attempt,
            "exit_code": 0,
            "outputs": [ExperimentFile(path="result.json", sha256=DIGEST, size_bytes=9)],
            **updates,
        }
    )


def test_append_query_export_and_fresh_replay(tmp_path: Path) -> None:
    path = tmp_path / "experiments.jsonl"
    store = ExperimentStore(path)
    first = start()
    final = after(first)
    store.append(first)
    store.append(final)
    content = store.export()
    entry = json.loads(content.splitlines()[0])
    assert entry["schema_version"] == EXPERIMENT_ENTRY_VERSION
    assert entry["event"]["schema_version"] == EXPERIMENT_EVENT_VERSION
    store.append(final)
    store.append(first)
    assert store.export() == content
    fresh = ExperimentStore(path)
    assert fresh.events() == (first, final)
    assert fresh.runs() == store.runs()
    assert fresh.runs()[0].definition == first.definition
    assert fresh.producers(path="result.json", sha256=DIGEST) == fresh.runs()
    assert fresh.producers(path="result.json", sha256="0" * 64) == ()
    assert fresh.producers(path="other.json", sha256=DIGEST) == ()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_identity_cannot_be_replaced(tmp_path: Path, terminal: str) -> None:
    store = ExperimentStore(tmp_path / "experiments.jsonl")
    first = start()
    final = after(first, status=terminal, exit_code=0 if terminal == "succeeded" else None)
    store.append(first)
    store.append(final)
    with pytest.raises(ValueError, match="terminal"):
        store.append(after(final))
    with pytest.raises(ValueError, match="started again"):
        store.append(start(run_id=first.run_id))
    with pytest.raises(ValueError, match="retry changed"):
        store.append(after(first, event_id=final.event_id, at=NOW + timedelta(seconds=3)))
    assert store.events() == (first, final)


def test_synchronization_skips_verified_retries_without_caching_between_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "experiments.jsonl"
    store = ExperimentStore(path)
    first = start()
    final = after(first)
    store.append(first)
    append = store.append
    appended: list[ExperimentEvent] = []

    def observe(event: ExperimentEvent) -> None:
        appended.append(event)
        append(event)

    monkeypatch.setattr(store, "append", observe)
    store.synchronize((first, final, final))
    assert appended == [final]
    before = path.read_bytes()
    store.synchronize((first, final))
    assert appended == [final]
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="retry changed"):
        store.synchronize((first, after(first, event_id=final.event_id, exit_code=None)))
    assert path.read_bytes() == before
    path.write_bytes(b"corrupted after last synchronization\n")
    with pytest.raises(ValueError, match="integrity recovery"):
        store.synchronize((first, final))


@pytest.mark.parametrize("changed_retry", [False, True])
def test_synchronization_keeps_append_authoritative_after_concurrent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_retry: bool
) -> None:
    path = tmp_path / "experiments.jsonl"
    store = ExperimentStore(path)
    first = start()
    final = after(first)
    store.append(first)
    concurrent = (
        after(first, event_id=final.event_id, at=final.at + timedelta(seconds=1))
        if changed_retry
        else final
    )
    snapshot = store.events

    def insert_after_snapshot() -> tuple[ExperimentEvent, ...]:
        events = snapshot()
        ExperimentStore(path).append(concurrent)
        return events

    monkeypatch.setattr(store, "events", insert_after_snapshot)
    if changed_retry:
        with pytest.raises(ValueError, match="retry changed"):
            store.synchronize((first, final))
    else:
        store.synchronize((first, final))
    assert ExperimentStore(path).events() == (first, concurrent)


def test_interruption_resume_and_cancellation_are_explicit(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.jsonl")
    first = start()
    interrupted = after(first, status="interrupted", outputs=(), exit_code=None)
    resumed = after(interrupted, status="resumed", attempt=2, outputs=(), exit_code=None)
    for event in (first, interrupted):
        store.append(event)
    assert ExperimentStore(tmp_path / "experiments.jsonl").runs()[0].status == "interrupted"
    with pytest.raises(ValueError, match="active attempt"):
        store.append(after(interrupted))
    store.append(resumed)
    store.append(after(resumed))
    run = store.runs()[0]
    assert run.run_id == first.run_id
    assert run.attempt == 2
    assert run.definition == first.definition
    assert run.status == "succeeded"
    abandoned = start()
    uncertain = after(abandoned, status="interrupted", outputs=(), exit_code=None)
    for event in (abandoned, uncertain, after(uncertain, status="cancelled", exit_code=None)):
        store.append(event)
    assert store.runs()[1].status == "cancelled"


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"at": NOW - timedelta(seconds=1)}, "backwards"),
        ({"attempt": 2}, "active attempt"),
        ({"status": "resumed", "outputs": (), "exit_code": None}, "Resume requires"),
        ({"outputs": ()}, "every declared output"),
        (
            {"outputs": [ExperimentFile(path="other.json", sha256=DIGEST, size_bytes=9)]},
            "undeclared",
        ),
    ],
)
def test_invalid_transitions_leave_journal_unchanged(
    tmp_path: Path, updates: dict[str, Any], match: str
) -> None:
    store = ExperimentStore(tmp_path / "experiments.jsonl")
    first = start()
    store.append(first)
    before = store.export()
    with pytest.raises(ValueError, match=match):
        store.append(after(first, **updates))
    assert store.export() == before


def test_orphan_and_duplicate_events_fail_closed(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.jsonl")
    first = start()
    with pytest.raises(ValueError, match="no start"):
        store.append(after(first))
    with pytest.raises(ValueError, match="Duplicate"):
        reduce_experiment_events((first, first))


@pytest.mark.parametrize("failure_point", ["before", "partial", "after", "cleanup"])
def test_existing_append_journal_recovers_without_duplicate_execution_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    path = tmp_path / "experiments.jsonl"
    store = ExperimentStore(path)
    first = start()
    final = after(first)
    store.append(first)
    original = files.append_private_bytes

    def fail_append(target: Path, content: bytes) -> None:
        if failure_point == "partial":
            original(target, content[: len(content) // 2])
        elif failure_point == "after":
            original(target, content)
        raise OSError("synthetic interruption")

    with monkeypatch.context() as patched:
        if failure_point == "cleanup":

            def fail_cleanup(_path: Path) -> None:
                raise OSError("synthetic interruption")

            patched.setattr(files, "unlink_durable", fail_cleanup)
        else:
            patched.setattr(files, "append_private_bytes", fail_append)
        with pytest.raises(OSError, match="synthetic interruption"):
            store.append(final)
    fresh = ExperimentStore(path)
    assert fresh.events() == (first, final)
    fresh.append(final)
    assert len(fresh.events()) == 2
    assert not path.with_name(f".{path.name}.pending").exists()


def test_concurrent_exact_retries_append_once(tmp_path: Path) -> None:
    path = tmp_path / "experiments.jsonl"
    first = start()
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: ExperimentStore(path).append(first), range(24)))
    assert ExperimentStore(path).events() == (first,)


@pytest.mark.parametrize("mutation", ["digest", "reorder", "duplicate", "unknown", "malformed"])
def test_corrupt_journal_is_not_rewritten(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "experiments.jsonl"
    store = ExperimentStore(path)
    first = start()
    store.append(first)
    store.append(after(first))
    lines = path.read_text().splitlines()
    if mutation == "digest":
        payload = json.loads(lines[1])
        payload["event"]["outputs"][0]["sha256"] = "0" * 64
        lines[1] = json.dumps(payload)
    elif mutation == "reorder":
        lines.reverse()
    elif mutation == "duplicate":
        lines.append(lines[-1])
    elif mutation == "unknown":
        payload = json.loads(lines[1])
        payload["event"]["secret"] = "must-not-appear-in-diagnostic"
        lines[1] = json.dumps(payload)
    else:
        lines[-1] = '{"secret": "must-not-appear-in-diagnostic"'
    path.write_text("\n".join(lines) + "\n")
    corrupted = path.read_bytes()
    for operation in (store.events, store.export, lambda: store.append(start())):
        with pytest.raises(ValueError, match=r"[Ee]xperiment") as error:
            operation()
        assert "must-not-appear" not in str(error.value)
        assert path.read_bytes() == corrupted


@pytest.mark.parametrize(
    "updates",
    [
        {"entry_point": "../outside.py"},
        {"entry_point": ".heartwood/keys"},
        {"entry_point": "missing.py"},
        {"output_paths": ["data.csv"]},
        {"output_paths": ["result.json", "RESULT.json"]},
        {"parameters": {"participant": "must-not-be-recorded"}},
        {"git_dirty": False},
        {"source": "heartwood"},
        {"actor_ref": "a\nsecond-line"},
    ],
)
def test_definition_rejects_unsafe_or_ambiguous_fields(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        definition(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"attempt": True},
        {"attempt": 2},
        {"at": datetime(2026, 9, 11)},
        {"exit_code": 0},
        {"outputs": [ExperimentFile(path="result.json", sha256=DIGEST, size_bytes=9)]},
        {"schema_version": "heartwood.experiment-event.v2"},
    ],
)
def test_event_rejects_invalid_begin_payload(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        start(**updates)


def test_model_copy_cannot_bypass_persistence_validation(tmp_path: Path) -> None:
    first = start()
    invalid = first.model_copy(update={"attempt": True})
    with pytest.raises(ValidationError):
        ExperimentStore(tmp_path / "experiments.jsonl").append(invalid)


def test_json_schema_and_export_have_no_content_fields(tmp_path: Path) -> None:
    schema = ExperimentEvent.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "heartwood.experiment-event.v1"
    store = ExperimentStore(tmp_path / "experiments.jsonl")
    store.append(start())
    payload = json.loads(store.export())["event"]["definition"]
    assert "parameters" not in payload
    assert "parameters_sha256" in payload
    assert all(set(item) == {"path", "sha256", "size_bytes"} for item in payload["inputs"])
