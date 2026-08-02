# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Compatibility and minimization tests for persisted OpenHands state."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path

import pytest
from openhands.sdk.event.conversation_error import ConversationErrorEvent

import heartwood.gateway._openhands_persistence as openhands_persistence
from heartwood.gateway._openhands_persistence import (
    ContentMinimizedLocalFileStore,
    OpenHandsPersistenceError,
)

_MARKER = ".heartwood-persistence.json"


def test_fresh_store_records_the_owned_schema_and_sdk_version(tmp_path: Path) -> None:
    root = tmp_path / "openhands"

    ContentMinimizedLocalFileStore(str(root))

    assert json.loads((root / _MARKER).read_text(encoding="utf-8")) == {
        "adopted_from": "new",
        "content_policy": "heartwood.openhands-content-minimized.v1",
        "openhands_sdk_version": version("openhands-sdk"),
        "schema_version": "heartwood.openhands-state.v1",
    }


def test_store_guards_compatibility_checks_with_a_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "openhands"
    lock_held = False
    observed: list[Path] = []
    original_ensure = ContentMinimizedLocalFileStore._ensure_compatible_state

    @contextmanager
    def observed_lock(path: Path, *, secure_parent: bool = True) -> Iterator[None]:
        nonlocal lock_held
        assert not secure_parent
        observed.append(path)
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def observed_ensure(store: ContentMinimizedLocalFileStore) -> None:
        assert lock_held
        original_ensure(store)

    monkeypatch.setattr(openhands_persistence, "native_file_lock", observed_lock)
    monkeypatch.setattr(ContentMinimizedLocalFileStore, "_ensure_compatible_state", observed_ensure)

    ContentMinimizedLocalFileStore(str(root))

    assert observed == [tmp_path / ".openhands.heartwood-migration.lock"]


def test_unversioned_marker_migrates_atomically_to_current_schema(tmp_path: Path) -> None:
    root = tmp_path / "openhands"
    root.mkdir()
    (root / "base_state.json").write_text("{}", encoding="utf-8")
    (root / _MARKER).write_text(
        json.dumps(
            {
                "schema_version": "heartwood.openhands-state.unversioned",
                "openhands_sdk_version": version("openhands-sdk"),
                "content_policy": "heartwood.openhands-content-minimized.v1",
            }
        ),
        encoding="utf-8",
    )

    ContentMinimizedLocalFileStore(str(root))

    marker = json.loads((root / _MARKER).read_text(encoding="utf-8"))
    assert marker["schema_version"] == "heartwood.openhands-state.v1"
    assert marker["adopted_from"] == "unversioned"


def test_markerless_state_is_typed_and_sensitive_errors_are_minimized(tmp_path: Path) -> None:
    root = tmp_path / "openhands"
    events = root / "events"
    events.mkdir(parents=True)
    event_path = events / "event-00000-12345678.json"
    event_path.write_text(
        ConversationErrorEvent(
            id="provider-error",
            source="environment",
            code="OpenAIError",
            detail="Incorrect API key: participant-secret",
        ).model_dump_json(exclude_none=True),
        encoding="utf-8",
    )

    ContentMinimizedLocalFileStore(str(root))

    persisted = event_path.read_text(encoding="utf-8")
    assert "participant-secret" not in persisted
    assert "Model provider authentication failed" in persisted
    assert json.loads((root / _MARKER).read_text(encoding="utf-8"))["adopted_from"] == (
        "unversioned"
    )


def test_store_rejects_sdk_mismatch_before_reading_conversation_state(tmp_path: Path) -> None:
    root = tmp_path / "openhands"
    ContentMinimizedLocalFileStore(str(root))
    marker_path = root / _MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["openhands_sdk_version"] = "0.0.0"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(OpenHandsPersistenceError, match="explicit SDK migration"):
        ContentMinimizedLocalFileStore(str(root))


def test_unversioned_sdk_mismatch_does_not_rewrite_existing_state(tmp_path: Path) -> None:
    root = tmp_path / "openhands"
    events = root / "events"
    events.mkdir(parents=True)
    marker_path = root / _MARKER
    event_path = events / "event-00000-12345678.json"
    marker_path.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.openhands-state.unversioned",
                "openhands_sdk_version": "0.0.0",
                "content_policy": "heartwood.openhands-content-minimized.v1",
            }
        ),
        encoding="utf-8",
    )
    event_path.write_text(
        ConversationErrorEvent(
            id="provider-error",
            source="environment",
            code="OpenAIError",
            detail="participant-secret",
        ).model_dump_json(exclude_none=True),
        encoding="utf-8",
    )
    marker_before = marker_path.read_bytes()
    event_before = event_path.read_bytes()

    with pytest.raises(OpenHandsPersistenceError, match="explicit SDK migration"):
        ContentMinimizedLocalFileStore(str(root))

    assert marker_path.read_bytes() == marker_before
    assert event_path.read_bytes() == event_before


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("gap", "sequence contains a gap"),
        ("malformed", "event state is malformed"),
        ("symlink", "symbolic link or special file"),
    ],
)
def test_markerless_store_rejects_incompatible_state(
    tmp_path: Path,
    failure: str,
    message: str,
) -> None:
    root = tmp_path / "openhands"
    events = root / "events"
    events.mkdir(parents=True)
    event = ConversationErrorEvent(
        id="provider-error",
        source="environment",
        code="OpenAIError",
        detail="private detail",
    ).model_dump_json(exclude_none=True)
    if failure == "gap":
        (events / "event-00001-12345678.json").write_text(event, encoding="utf-8")
    elif failure == "malformed":
        (events / "event-00000-12345678.json").write_text("{", encoding="utf-8")
    else:
        outside = tmp_path / "outside.json"
        outside.write_text(event, encoding="utf-8")
        (events / "event-00000-12345678.json").symlink_to(outside)

    with pytest.raises(OpenHandsPersistenceError, match=message):
        ContentMinimizedLocalFileStore(str(root))


def test_store_rejects_malformed_event_without_exposing_content(tmp_path: Path) -> None:
    store = ContentMinimizedLocalFileStore(str(tmp_path / "openhands"))

    with pytest.raises(OpenHandsPersistenceError) as captured:
        store.write("events/event-00000-12345678.json", "participant-secret")

    assert "participant-secret" not in str(captured.value)


def test_store_writes_binary_state_atomically(tmp_path: Path) -> None:
    root = tmp_path / "openhands"
    store = ContentMinimizedLocalFileStore(str(root))

    store.write("binary-state", b"synthetic\x00state")

    assert (root / "binary-state").read_bytes() == b"synthetic\x00state"


def test_binary_write_invalidates_cached_text(tmp_path: Path) -> None:
    store = ContentMinimizedLocalFileStore(str(tmp_path / "openhands"))
    store.write("state", "cached text")
    assert store.read("state") == "cached text"

    store.write("state", b"\xff")

    with pytest.raises(UnicodeDecodeError):
        store.read("state")


@pytest.mark.parametrize(
    ("marker_update", "message"),
    [
        ({"unexpected": "field"}, "marker is malformed"),
        ({"schema_version": "heartwood.openhands-state.v99"}, "schema is unsupported"),
        ({"content_policy": "unsupported"}, "content policy is unsupported"),
        ({"adopted_from": "unknown"}, "origin is unsupported"),
    ],
)
def test_store_rejects_incompatible_current_markers(
    tmp_path: Path,
    marker_update: dict[str, str],
    message: str,
) -> None:
    root = tmp_path / "openhands"
    ContentMinimizedLocalFileStore(str(root))
    marker_path = root / _MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker.update(marker_update)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(OpenHandsPersistenceError, match=message):
        ContentMinimizedLocalFileStore(str(root))


@pytest.mark.parametrize("base_state", ["[]", "{"])
def test_markerless_store_rejects_malformed_base_state(
    tmp_path: Path,
    base_state: str,
) -> None:
    root = tmp_path / "openhands"
    root.mkdir()
    (root / "base_state.json").write_text(base_state, encoding="utf-8")

    with pytest.raises(OpenHandsPersistenceError, match="base state"):
        ContentMinimizedLocalFileStore(str(root))


def test_markerless_store_rejects_unsupported_event_filename(tmp_path: Path) -> None:
    events = tmp_path / "openhands" / "events"
    events.mkdir(parents=True)
    (events / "event-latest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(OpenHandsPersistenceError, match="filename is unsupported"):
        ContentMinimizedLocalFileStore(str(tmp_path / "openhands"))


def test_store_normalizes_filesystem_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "openhands"
    root.mkdir()
    state = root / "base_state.json"
    state.write_text("{}", encoding="utf-8")
    original_chmod = Path.chmod

    def unavailable(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if path == state:
            raise OSError("synthetic unavailable entry")
        original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", unavailable)

    with pytest.raises(OpenHandsPersistenceError, match="entry is unavailable"):
        ContentMinimizedLocalFileStore(str(root))
