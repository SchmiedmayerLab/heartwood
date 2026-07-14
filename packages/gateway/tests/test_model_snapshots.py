# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from heartwood.gateway import (
    ModelSnapshot,
    ModelSnapshotError,
    download_model_snapshot,
    load_model_snapshot_catalog,
    verify_model_snapshot,
)


def test_repository_snapshot_catalog_pins_the_carina_demo_model() -> None:
    catalog = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )

    snapshot = catalog.snapshot("qwen25-7b-instruct-vllm")
    assert snapshot.runtime_profile == "vllm-cuda"
    assert snapshot.source_repository == "Qwen/Qwen2.5-7B-Instruct"
    assert snapshot.source_revision == "a09a35458c702b33eeacc393d103063234e8bc28"
    assert snapshot.minimum_free_bytes >= snapshot.expected_size_bytes


def test_snapshot_download_is_atomic_and_creates_exact_provenance(tmp_path: Path) -> None:
    snapshot = _snapshot()
    progress: list[tuple[int, int]] = []

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        (local_dir / ".cache" / "huggingface").mkdir(parents=True)
        (local_dir / ".cache" / "huggingface" / "download.lock").touch()
        return str(local_dir)

    destination = download_model_snapshot(
        snapshot,
        cache_dir=tmp_path / "models",
        downloader=downloader,
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert destination == tmp_path / "models" / snapshot.snapshot_id
    assert not (destination / ".cache").exists()
    source = json.loads((destination / "HEARTWOOD-SOURCE.json").read_text(encoding="utf-8"))
    assert source["source_revision"] == snapshot.source_revision
    verify_model_snapshot(destination)
    assert (tmp_path / "models" / f".{snapshot.snapshot_id}.lock").is_file()
    assert not any(
        path.is_dir() for path in (tmp_path / "models").glob(f".{snapshot.snapshot_id}.*")
    )
    assert progress[0] == (0, snapshot.expected_size_bytes)
    assert progress[-1] == (snapshot.expected_size_bytes, snapshot.expected_size_bytes)


def test_snapshot_download_reuses_verified_content_and_rejects_tampering(tmp_path: Path) -> None:
    snapshot = _snapshot()
    calls = 0

    def downloader(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        return str(local_dir)

    destination = download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    reused = download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    assert reused == destination
    assert calls == 1

    (destination / "weights.safetensors").write_bytes(b"modified")
    with pytest.raises(ModelSnapshotError, match="incomplete or modified"):
        download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)


def test_snapshot_download_serializes_concurrent_callers(tmp_path: Path) -> None:
    snapshot = _snapshot()
    started = Event()
    release = Event()
    calls = 0

    def downloader(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        started.set()
        assert release.wait(timeout=5)
        return str(local_dir)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            download_model_snapshot,
            snapshot,
            cache_dir=tmp_path,
            downloader=downloader,
        )
        assert started.wait(timeout=5)
        second = executor.submit(
            download_model_snapshot,
            snapshot,
            cache_dir=tmp_path,
            downloader=downloader,
        )
        release.set()

        assert first.result(timeout=5) == second.result(timeout=5)
    assert calls == 1


def test_snapshot_download_rejects_an_existing_different_revision(tmp_path: Path) -> None:
    snapshot = _snapshot()

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        return str(local_dir)

    download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    different_revision = replace(snapshot, source_revision="f" * 40)

    with pytest.raises(ModelSnapshotError, match="incomplete or modified"):
        download_model_snapshot(
            different_revision,
            cache_dir=tmp_path,
            downloader=downloader,
        )


def test_snapshot_download_rejects_content_outside_reviewed_size(tmp_path: Path) -> None:
    snapshot = replace(_snapshot(), expected_size_bytes=10, minimum_free_bytes=10)

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"x" * 100)
        return str(local_dir)

    with pytest.raises(ModelSnapshotError, match="outside the reviewed range"):
        download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    assert not any(path.is_dir() for path in tmp_path.glob(f".{snapshot.snapshot_id}.*"))


def test_snapshot_metadata_rejects_floating_revisions() -> None:
    with pytest.raises(ModelSnapshotError, match="immutable commit revision"):
        replace(_snapshot(), source_revision="main").validate()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"snapshot_id": "../unsafe"}, "safe cache directory"),
        ({"source_repository": "not-a-repository"}, "owner/repository"),
        ({"purpose": ""}, "purpose must be"),
        ({"expected_size_bytes": 0}, "storage metadata"),
        ({"minimum_free_bytes": 1}, "storage metadata"),
    ],
)
def test_snapshot_metadata_rejects_unsafe_values(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ModelSnapshotError, match=message):
        replace(_snapshot(), **changes).validate()  # type: ignore[arg-type]


def test_snapshot_catalog_reports_unknown_ids_and_invalid_documents(tmp_path: Path) -> None:
    catalog = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )
    with pytest.raises(ModelSnapshotError, match="unknown model snapshot"):
        catalog.snapshot("missing")

    missing = tmp_path / "missing.toml"
    with pytest.raises(ModelSnapshotError, match="unable to load"):
        load_model_snapshot_catalog(missing)

    invalid_schema = tmp_path / "schema.toml"
    invalid_schema.write_text('schema_version = "unsupported"\n[snapshots]\n', encoding="utf-8")
    with pytest.raises(ModelSnapshotError, match="unsupported"):
        load_model_snapshot_catalog(invalid_schema)

    missing_snapshots = tmp_path / "missing-snapshots.toml"
    missing_snapshots.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="snapshots table"):
        load_model_snapshot_catalog(missing_snapshots)

    invalid_entry = tmp_path / "entry.toml"
    invalid_entry.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v1"\n[snapshots]\ninvalid = "value"\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="entries must be tables"):
        load_model_snapshot_catalog(invalid_entry)

    invalid_fields = tmp_path / "fields.toml"
    invalid_fields.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v1"\n'
        "[snapshots.invalid]\n"
        'runtime_profile = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="runtime_profile"):
        load_model_snapshot_catalog(invalid_fields)


def test_snapshot_download_checks_capacity_and_uses_hugging_face_downloader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot()
    monkeypatch.setattr(
        "heartwood.gateway._model_snapshots.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )
    with pytest.raises(ModelSnapshotError, match="requires at least"):
        download_model_snapshot(snapshot, cache_dir=tmp_path / "small")

    monkeypatch.setattr(
        "heartwood.gateway._model_snapshots.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100, used=0, free=100),
    )
    calls: list[dict[str, object]] = []

    def snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        return str(local_dir)

    monkeypatch.setattr(
        "heartwood.gateway._model_snapshots.import_module",
        lambda _name: SimpleNamespace(snapshot_download=snapshot_download),
    )

    destination = download_model_snapshot(snapshot, cache_dir=tmp_path / "enough")

    assert destination.is_dir()
    assert calls[0]["repo_id"] == snapshot.source_repository
    assert calls[0]["revision"] == snapshot.source_revision


def test_snapshot_download_rejects_existing_content_without_source_record(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    destination = tmp_path / snapshot.snapshot_id
    destination.mkdir()
    content = b"synthetic-weights"
    (destination / "weights.safetensors").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    (destination / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")

    with pytest.raises(ModelSnapshotError, match="incomplete or modified"):
        download_model_snapshot(snapshot, cache_dir=tmp_path)


def test_snapshot_verifier_rejects_a_linked_root(tmp_path: Path) -> None:
    root = tmp_path / "real"
    root.mkdir()
    (root / "file").write_text("content", encoding="utf-8")
    (root / "SHA256SUMS").write_text(
        "ed7002b439e9ac845f22357d822bac144473aa7c2d8b7b3e36726061b4a93f03  file\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)

    with pytest.raises(ValueError, match="regular SHA256SUMS"):
        verify_model_snapshot(linked)


def _snapshot() -> ModelSnapshot:
    return ModelSnapshot(
        snapshot_id="synthetic-vllm",
        runtime_profile="vllm-cuda",
        purpose="Synthetic test snapshot.",
        source_repository="example/model",
        source_revision="0123456789abcdef0123456789abcdef01234567",
        expected_size_bytes=20,
        minimum_free_bytes=20,
        license_posture="Synthetic test content only.",
        model_alias="Synthetic vLLM",
    )


def _repo_root() -> Path:
    return Path(__file__).parents[3]
