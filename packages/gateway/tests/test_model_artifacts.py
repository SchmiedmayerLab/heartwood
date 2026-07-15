# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest

from heartwood.gateway import (
    LocalModelDownloadManager,
    ModelArtifact,
    ModelArtifactCatalog,
    ModelArtifactError,
    ModelSnapshot,
    ModelSnapshotCatalog,
    download_model_artifact,
    load_model_artifact_catalog,
)


def test_repository_catalog_contains_only_explicit_download_artifacts() -> None:
    catalog = load_model_artifact_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "model-catalog.toml"
    )

    assert {artifact.artifact_id for artifact in catalog.artifacts} == {
        "llama-cpp-stories260k-ci",
        "qwen25-7b-instruct-q4_k_m",
        "qwen25-coder-7b-instruct-q4_k_m",
    }
    assert all(artifact.source_revision not in {"main", "latest"} for artifact in catalog.artifacts)
    assert catalog.artifact("qwen25-7b-instruct-q4_k_m").context_window == 32_768


def test_artifact_download_verifies_size_and_checksum(tmp_path: Path) -> None:
    content = b"reviewed-model-artifact"
    artifact = _artifact(content)

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        path = local_dir / "model.gguf"
        path.write_bytes(content)
        return str(path)

    path = download_model_artifact(
        artifact,
        cache_dir=tmp_path / "models",
        downloader=downloader,
    )

    assert path.read_bytes() == content


def test_artifact_download_reports_transferred_bytes(tmp_path: Path) -> None:
    content = b"reviewed-model-artifact"
    artifact = _artifact(content)
    progress: list[tuple[int, int]] = []

    def downloader(**kwargs: object) -> str:
        progress_class = cast(type[Any], kwargs["tqdm_class"])
        with progress_class(total=len(content), initial=0) as transfer:
            transfer.update(8)
            transfer.update(len(content) - 8)
        path = Path(str(kwargs["local_dir"])) / "model.gguf"
        path.write_bytes(content)
        return str(path)

    download_model_artifact(
        artifact,
        cache_dir=tmp_path / "models",
        downloader=downloader,
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert (8, len(content)) in progress
    assert progress[-1] == (len(content), len(content))


def test_artifact_download_rejects_integrity_mismatch(tmp_path: Path) -> None:
    artifact = _artifact(b"expected")

    def downloader(**kwargs: object) -> str:
        path = Path(str(kwargs["local_dir"])) / "model.gguf"
        path.write_bytes(b"tampered")
        return str(path)

    with pytest.raises(ModelArtifactError, match="does not match"):
        download_model_artifact(
            artifact,
            cache_dir=tmp_path / "models",
            downloader=downloader,
        )
    assert not (tmp_path / "models" / artifact.artifact_id).exists()


def test_artifact_download_reuses_verified_installation(tmp_path: Path) -> None:
    content = b"reviewed-model-artifact"
    artifact = _artifact(content)
    installed = tmp_path / "models" / artifact.artifact_id / artifact.source_path
    installed.parent.mkdir(parents=True)
    installed.write_bytes(content)
    progress: list[tuple[int, int]] = []

    path = download_model_artifact(
        artifact,
        cache_dir=tmp_path / "models",
        downloader=lambda **_kwargs: pytest.fail("verified artifacts must not be downloaded again"),
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert path == installed
    assert progress == [(len(content), len(content))]


def test_artifact_download_rejects_modified_existing_installation(tmp_path: Path) -> None:
    artifact = _artifact(b"reviewed-model-artifact")
    installed = tmp_path / "models" / artifact.artifact_id / artifact.source_path
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"modified")

    with pytest.raises(ModelArtifactError, match="existing model artifact is incomplete"):
        download_model_artifact(
            artifact,
            cache_dir=tmp_path / "models",
            downloader=lambda **_kwargs: pytest.fail("modified artifacts must not be overwritten"),
        )


def test_artifact_download_checks_free_space_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(b"reviewed-model-artifact")
    monkeypatch.setattr(
        "heartwood.gateway._model_artifacts.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=artifact.minimum_free_bytes - 1),
    )

    with pytest.raises(ModelArtifactError, match=r"requires at least.*available"):
        download_model_artifact(
            artifact,
            cache_dir=tmp_path / "models",
            downloader=lambda **_kwargs: pytest.fail("download must not start"),
        )


def test_artifact_metadata_rejects_floating_revisions() -> None:
    artifact = _artifact(b"content")

    with pytest.raises(ModelArtifactError, match="immutable revision"):
        replace(artifact, source_revision="main").validate()


def test_background_manager_reports_ready_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(b"content")
    catalog = ModelArtifactCatalog(
        schema_version="heartwood.local-model-catalog.v1",
        artifacts=(artifact,),
    )
    installed = tmp_path / "models" / artifact.artifact_id / "model.gguf"

    def download(
        _artifact: ModelArtifact,
        *,
        cache_dir: Path,
        progress_callback: Callable[[int, int], None],
    ) -> Path:
        assert cache_dir == tmp_path / "models"
        progress_callback(len(b"content"), len(b"content"))
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"content")
        return installed

    monkeypatch.setattr("heartwood.gateway._model_artifacts.download_model_artifact", download)
    selected: list[tuple[str, Path, str]] = []
    manager = LocalModelDownloadManager(
        artifact_catalog=catalog,
        snapshot_catalog=_empty_snapshot_catalog(),
        cache_dir=tmp_path / "models",
        on_ready=lambda model_id, path, runtime: selected.append((model_id, path, runtime)),
    )

    assert manager.start(artifact.artifact_id).status == "downloading"
    deadline = time.monotonic() + 2
    while manager.statuses()[0].status == "downloading" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert manager.statuses()[0].status == "ready"
    assert manager.statuses()[0].bytes_downloaded == len(b"content")
    assert manager.statuses()[0].bytes_total == len(b"content")
    assert manager.statuses()[0].path == str(installed)
    assert selected == [(artifact.artifact_id, installed, artifact.runtime_profile)]
    assert manager.start(artifact.artifact_id).status == "ready"


def test_background_manager_exposes_in_progress_byte_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(b"content")
    catalog = ModelArtifactCatalog(
        schema_version="heartwood.local-model-catalog.v1",
        artifacts=(artifact,),
    )
    started = Event()
    release = Event()

    def download(
        _artifact: ModelArtifact,
        *,
        cache_dir: Path,
        progress_callback: Callable[[int, int], None],
    ) -> Path:
        progress_callback(3, len(b"content"))
        progress_callback(1, len(b"content"))
        started.set()
        assert release.wait(timeout=2)
        path = cache_dir / artifact.artifact_id / "model.gguf"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"content")
        return path

    monkeypatch.setattr("heartwood.gateway._model_artifacts.download_model_artifact", download)
    manager = LocalModelDownloadManager(
        artifact_catalog=catalog,
        snapshot_catalog=_empty_snapshot_catalog(),
        cache_dir=tmp_path / "models",
        on_ready=lambda _model_id, _path, _runtime: None,
    )

    manager.start(artifact.artifact_id)
    assert started.wait(timeout=2)
    status = manager.statuses()[0]
    assert status.status == "downloading"
    assert status.bytes_downloaded == 3
    assert status.bytes_total == len(b"content")
    release.set()
    deadline = time.monotonic() + 2
    while manager.statuses()[0].status == "downloading" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.statuses()[0].status == "ready"


def test_background_manager_reports_actionable_safe_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(b"content")
    catalog = ModelArtifactCatalog(
        schema_version="heartwood.local-model-catalog.v1",
        artifacts=(artifact,),
    )

    def rejected_download(
        _artifact: ModelArtifact,
        *,
        cache_dir: Path,
        progress_callback: Callable[[int, int], None],
    ) -> Path:
        progress_callback(1, artifact.artifact_size_bytes)
        raise ModelArtifactError(f"not enough project storage under {cache_dir}")

    monkeypatch.setattr(
        "heartwood.gateway._model_artifacts.download_model_artifact",
        rejected_download,
    )
    manager = LocalModelDownloadManager(
        artifact_catalog=catalog,
        snapshot_catalog=_empty_snapshot_catalog(),
        cache_dir=tmp_path / "models",
        on_ready=lambda _model_id, _path, _runtime: None,
    )

    manager.start(artifact.artifact_id)
    deadline = time.monotonic() + 2
    while manager.statuses()[0].status == "downloading" and time.monotonic() < deadline:
        time.sleep(0.01)

    status = manager.statuses()[0]
    assert status.status == "error"
    assert status.bytes_downloaded == 1
    assert status.error == f"not enough project storage under {tmp_path / 'models'}"


def test_background_manager_downloads_and_selects_a_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = ModelSnapshot(
        snapshot_id="test-snapshot",
        runtime_profile="vllm-cuda",
        purpose="Synthetic snapshot",
        source_repository="example/test-snapshot",
        source_revision="a" * 40,
        expected_size_bytes=7,
        minimum_free_bytes=7,
        license_posture="Synthetic",
        model_alias="Test snapshot",
    )
    installed = tmp_path / "models" / snapshot.snapshot_id

    def download(
        _snapshot: ModelSnapshot,
        *,
        cache_dir: Path,
        progress_callback: Callable[[int, int], None],
    ) -> Path:
        progress_callback(4, 7)
        path = cache_dir / snapshot.snapshot_id
        path.mkdir(parents=True)
        return path

    monkeypatch.setattr("heartwood.gateway._model_artifacts.download_model_snapshot", download)
    selected: list[tuple[str, Path, str]] = []
    manager = LocalModelDownloadManager(
        artifact_catalog=ModelArtifactCatalog(
            schema_version="heartwood.local-model-catalog.v1",
            artifacts=(),
        ),
        snapshot_catalog=ModelSnapshotCatalog(
            schema_version="heartwood.model-snapshot-catalog.v1",
            snapshots=(snapshot,),
        ),
        cache_dir=tmp_path / "models",
        on_ready=lambda model_id, path, runtime: selected.append((model_id, path, runtime)),
    )

    assert manager.start(snapshot.snapshot_id).model_id == snapshot.snapshot_id
    deadline = time.monotonic() + 2
    while manager.statuses()[0].status == "downloading" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert manager.statuses()[0].status == "ready"
    assert manager.statuses()[0].path == str(installed)
    assert selected == [(snapshot.snapshot_id, installed, snapshot.runtime_profile)]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"artifact_id": "../model"}, "artifact_id"),
        ({"source_repository": "repository"}, "source_repository"),
        ({"source_path": "/model.gguf"}, "source_path"),
        ({"source_path": "../model.gguf"}, "source_path"),
        ({"source_revision": "latest"}, "immutable revision"),
        ({"artifact_size_bytes": 0}, "storage metadata"),
        ({"minimum_free_bytes": 1}, "storage metadata"),
        ({"artifact_sha256": "ABC"}, "lowercase SHA-256"),
    ],
)
def test_artifact_metadata_rejects_unsafe_values(
    changes: dict[str, object],
    message: str,
) -> None:
    artifact = replace(_artifact(b"content"), **cast(Any, changes))

    with pytest.raises(ModelArtifactError, match=message):
        artifact.validate()


def test_catalog_lookup_and_safe_serialization() -> None:
    artifact = _artifact(b"content")
    catalog = ModelArtifactCatalog(
        schema_version="heartwood.local-model-catalog.v1",
        artifacts=(artifact,),
    )

    assert catalog.artifact("test-model") == artifact
    assert catalog.safe_dict()["artifacts"] == [artifact.safe_dict()]
    with pytest.raises(ModelArtifactError, match="unknown model artifact"):
        catalog.artifact("missing")


def test_catalog_loader_rejects_malformed_catalogs_and_manifests(tmp_path: Path) -> None:
    catalog_path = tmp_path / "a" / "b" / "c" / "model-catalog.toml"
    catalog_path.parent.mkdir(parents=True)

    with pytest.raises(ModelArtifactError, match="unable to load"):
        load_model_artifact_catalog(catalog_path)

    catalog_path.write_text('schema_version = "unsupported"\n', encoding="utf-8")
    with pytest.raises(ModelArtifactError, match="unsupported"):
        load_model_artifact_catalog(catalog_path)

    catalog_path.write_text(
        'schema_version = "heartwood.local-model-catalog.v1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelArtifactError, match="models table"):
        load_model_artifact_catalog(catalog_path)

    catalog_path.write_text(
        "\n".join(
            (
                'schema_version = "heartwood.local-model-catalog.v1"',
                "[models.invalid]",
                'artifact_manifest = "manifest.toml"',
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "manifest.toml").write_text("not = [valid", encoding="utf-8")
    with pytest.raises(ModelArtifactError, match="unable to load model artifact manifest"):
        load_model_artifact_catalog(catalog_path)


def test_catalog_loader_rejects_duplicate_artifact_ids(tmp_path: Path) -> None:
    catalog_path = tmp_path / "a" / "b" / "c" / "model-catalog.toml"
    catalog_path.parent.mkdir(parents=True)
    (tmp_path / "one.toml").write_text(_artifact_manifest("same"), encoding="utf-8")
    (tmp_path / "two.toml").write_text(_artifact_manifest("same"), encoding="utf-8")
    catalog_path.write_text(
        "\n".join(
            (
                'schema_version = "heartwood.local-model-catalog.v1"',
                "[models.one]",
                'artifact_manifest = "one.toml"',
                "[models.ignored]",
                'status = "candidate"',
                "[models.two]",
                'artifact_manifest = "two.toml"',
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModelArtifactError, match="must be unique"):
        load_model_artifact_catalog(catalog_path)


def test_download_rejects_missing_outside_and_wrong_size_paths(tmp_path: Path) -> None:
    artifact = _artifact(b"expected")

    def missing(**_kwargs: object) -> str:
        return str(tmp_path / "models" / "test-model" / "missing.gguf")

    with pytest.raises(ModelArtifactError, match="missing"):
        download_model_artifact(artifact, cache_dir=tmp_path / "models", downloader=missing)

    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"expected")

    def escaped(**_kwargs: object) -> str:
        return str(outside)

    with pytest.raises(ModelArtifactError, match="escapes"):
        download_model_artifact(artifact, cache_dir=tmp_path / "models", downloader=escaped)

    def wrong_source(**kwargs: object) -> str:
        path = Path(str(kwargs["local_dir"])) / "other.gguf"
        path.write_bytes(b"expected")
        return str(path)

    with pytest.raises(ModelArtifactError, match="does not match its pinned source"):
        download_model_artifact(
            artifact,
            cache_dir=tmp_path / "models",
            downloader=wrong_source,
        )

    def wrong_size(**kwargs: object) -> str:
        path = Path(str(kwargs["local_dir"])) / "model.gguf"
        path.write_bytes(b"short")
        return str(path)

    with pytest.raises(ModelArtifactError, match="size does not match"):
        download_model_artifact(
            artifact,
            cache_dir=tmp_path / "models",
            downloader=wrong_size,
        )


def test_default_hugging_face_downloader_is_resolved_lazily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"content"
    artifact = _artifact(content)

    def downloader(**kwargs: object) -> str:
        path = Path(str(kwargs["local_dir"])) / "model.gguf"
        path.write_bytes(content)
        return str(path)

    monkeypatch.setattr(
        "heartwood.gateway._model_artifacts.import_module",
        lambda _name: SimpleNamespace(hf_hub_download=downloader),
    )

    assert download_model_artifact(artifact, cache_dir=tmp_path / "models").is_file()


def _artifact_manifest(artifact_id: str) -> str:
    digest = hashlib.sha256(b"content").hexdigest()
    return "\n".join(
        (
            'schema_version = "1"',
            f'artifact_id = "{artifact_id}"',
            'runtime_profile = "llama-cpp-cpu"',
            'purpose = "Synthetic test"',
            'source_repository = "example/model"',
            'source_path = "model.gguf"',
            'source_revision = "0123456789abcdef"',
            'artifact_format = "GGUF"',
            "artifact_size_bytes = 7",
            "minimum_free_bytes = 11",
            f'artifact_sha256 = "{digest}"',
            'license_posture = "Synthetic"',
            'model_alias = "test"',
            "context_window = 16384",
        )
    )


def _artifact(content: bytes) -> ModelArtifact:
    return ModelArtifact(
        artifact_id="test-model",
        runtime_profile="llama-cpp-cpu",
        purpose="Synthetic unit-test artifact.",
        source_repository="example/test-model",
        source_path="model.gguf",
        source_revision="0123456789abcdef",
        artifact_format="GGUF",
        artifact_size_bytes=len(content),
        minimum_free_bytes=(len(content) * 3 + 1) // 2,
        artifact_sha256=hashlib.sha256(content).hexdigest(),
        license_posture="Synthetic test fixture.",
        model_alias="test-model",
    )


def _empty_snapshot_catalog() -> ModelSnapshotCatalog:
    return ModelSnapshotCatalog(
        schema_version="heartwood.model-snapshot-catalog.v1",
        snapshots=(),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
