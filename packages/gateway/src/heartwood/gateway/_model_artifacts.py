# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Recommended local-model artifacts downloaded to mounted runtime storage."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, cast

from filelock import FileLock

from heartwood.gateway._model_identity import (
    is_hugging_face_model_id,
    is_immutable_revision,
)
from heartwood.gateway._model_snapshots import (
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelSnapshotError,
    download_model_snapshot,
)

type DownloadStatus = Literal["downloading", "error", "ready"]
type ProgressCallback = Callable[[int, int], None]
type DownloadReadyCallback = Callable[[str, Path, str], None]


class _DownloadProgress:
    """Minimal Hugging Face progress adapter that emits byte counts."""

    _default_total = 0

    def __init__(
        self,
        *,
        total: int | None = None,
        initial: int = 0,
        **_kwargs: object,
    ) -> None:
        self.total = total or self._default_total
        self.n = initial
        self._report(self.n, self.total)

    @staticmethod
    def _report(_downloaded: int, _total: int) -> None:
        return None

    def update(self, amount: int = 1) -> None:
        self.n = max(0, self.n + amount)
        self._report(self.n, self.total)

    def close(self) -> None:
        self._report(self.n, self.total)

    def __enter__(self) -> _DownloadProgress:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class ArtifactDownloader(Protocol):
    """Callable contract implemented by ``huggingface_hub.hf_hub_download``."""

    def __call__(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str,
        local_dir: Path,
        tqdm_class: type[_DownloadProgress] | None = None,
    ) -> str: ...


class ModelArtifactError(ValueError):
    """Raised when artifact metadata or a downloaded file is invalid."""


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Pinned Hugging Face artifact metadata."""

    artifact_id: str
    runtime_profile: str
    purpose: str
    source_repository: str
    source_path: str
    source_revision: str
    artifact_format: str
    artifact_size_bytes: int
    minimum_free_bytes: int
    artifact_sha256: str
    license_posture: str
    model_alias: str
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None
    recommended: bool = False

    def validate(self) -> None:
        """Validate pinned identity and integrity metadata."""
        if not self.artifact_id or "/" in self.artifact_id or ".." in self.artifact_id:
            msg = "artifact_id must be a safe cache directory name"
            raise ModelArtifactError(msg)
        if not is_hugging_face_model_id(self.source_repository):
            msg = "source_repository must be a Hugging Face owner/repository id"
            raise ModelArtifactError(msg)
        if (
            not self.source_path
            or Path(self.source_path).is_absolute()
            or ".." in Path(self.source_path).parts
        ):
            msg = "source_path must be a safe repository-relative path"
            raise ModelArtifactError(msg)
        if not is_immutable_revision(self.source_revision):
            msg = "source_revision must be an immutable revision"
            raise ModelArtifactError(msg)
        if self.artifact_size_bytes <= 0 or self.minimum_free_bytes < self.artifact_size_bytes:
            msg = "artifact storage metadata is invalid"
            raise ModelArtifactError(msg)
        if len(self.artifact_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.artifact_sha256
        ):
            msg = "artifact_sha256 must be a lowercase SHA-256 digest"
            raise ModelArtifactError(msg)

    def safe_dict(self) -> dict[str, object]:
        """Return artifact metadata safe for APIs."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelArtifactCatalog:
    """Recommended downloadable artifacts keyed by stable id."""

    schema_version: str
    artifacts: tuple[ModelArtifact, ...]

    def artifact(self, artifact_id: str) -> ModelArtifact:
        """Return one artifact from the repository recommendation catalog."""
        for artifact in self.artifacts:
            if artifact.artifact_id == artifact_id:
                return artifact
        msg = f"unknown model artifact: {artifact_id}"
        raise ModelArtifactError(msg)

    def safe_dict(self) -> dict[str, object]:
        """Return serializable catalog metadata."""
        return {
            "schema_version": self.schema_version,
            "artifacts": [artifact.safe_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True, slots=True)
class ModelDownload:
    """Background download status without secret or prompt content."""

    model_id: str
    status: DownloadStatus
    bytes_downloaded: int
    bytes_total: int
    path: str | None = None
    error: str | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return serializable download status."""
        return asdict(self)


class LocalModelDownloadManager:
    """Download, verify, and select local models in the background."""

    def __init__(
        self,
        *,
        artifact_catalog: ModelArtifactCatalog,
        snapshot_catalog: ModelSnapshotCatalog,
        cache_dir: Path,
        on_ready: DownloadReadyCallback,
    ) -> None:
        self.artifact_catalog = artifact_catalog
        self.snapshot_catalog = snapshot_catalog
        self.cache_dir = cache_dir
        self.on_ready = on_ready
        self._downloads: dict[str, ModelDownload] = {}
        self._lock = threading.Lock()

    def start(self, model_id: str) -> ModelDownload:
        """Start or return one recommended artifact or snapshot download."""
        model, _total = self._model(model_id)
        return self.start_model(model)

    def start_model(self, model: ModelArtifact | ModelSnapshot) -> ModelDownload:
        """Start or return one validated model supplied by the gateway catalog."""
        model.validate()
        model_id, total, _runtime_profile = self._metadata(model)
        with self._lock:
            current = self._downloads.get(model_id)
            if current is not None and current.status in {"downloading", "ready"}:
                return current
            download = ModelDownload(
                model_id=model_id,
                status="downloading",
                bytes_downloaded=0,
                bytes_total=total,
            )
            self._downloads[model_id] = download
        thread = threading.Thread(
            target=self._download,
            args=(model,),
            daemon=True,
            name=f"heartwood-model-{model_id}",
        )
        thread.start()
        return download

    def statuses(self) -> tuple[ModelDownload, ...]:
        """Return stable snapshots of current downloads."""
        with self._lock:
            return tuple(self._downloads.values())

    def _download(self, model: ModelArtifact | ModelSnapshot) -> None:
        model_id, total, runtime_profile = self._metadata(model)
        try:
            if isinstance(model, ModelArtifact):
                path = download_model_artifact(
                    model,
                    cache_dir=self.cache_dir,
                    progress_callback=lambda downloaded, _total: self._record_progress(
                        model_id, downloaded, total
                    ),
                )
            else:
                path = download_model_snapshot(
                    model,
                    cache_dir=self.cache_dir,
                    progress_callback=lambda downloaded, _total: self._record_progress(
                        model_id, downloaded, total
                    ),
                )
            self.on_ready(model_id, path, runtime_profile)
            result = ModelDownload(
                model_id=model_id,
                status="ready",
                bytes_downloaded=total,
                bytes_total=total,
                path=str(path),
            )
        except Exception as error:  # pragma: no cover - network failures vary by environment
            with self._lock:
                downloaded = self._downloads[model_id].bytes_downloaded
                self._downloads[model_id] = ModelDownload(
                    model_id=model_id,
                    status="error",
                    bytes_downloaded=downloaded,
                    bytes_total=total,
                    error=_safe_download_error(error),
                )
            return
        with self._lock:
            self._downloads[model_id] = result

    def _record_progress(self, model_id: str, downloaded: int, total: int) -> None:
        with self._lock:
            current = self._downloads.get(model_id)
            if current is None or current.status != "downloading":
                return
            self._downloads[model_id] = ModelDownload(
                model_id=model_id,
                status="downloading",
                bytes_downloaded=min(max(downloaded, current.bytes_downloaded, 0), total),
                bytes_total=total,
            )

    def _model(self, model_id: str) -> tuple[ModelArtifact | ModelSnapshot, int]:
        try:
            artifact = self.artifact_catalog.artifact(model_id)
        except ModelArtifactError:
            try:
                snapshot = self.snapshot_catalog.snapshot(model_id)
            except ModelSnapshotError as error:
                raise ModelArtifactError(f"unknown recommended local model: {model_id}") from error
            return snapshot, snapshot.expected_size_bytes
        return artifact, artifact.artifact_size_bytes

    @staticmethod
    def _metadata(model: ModelArtifact | ModelSnapshot) -> tuple[str, int, str]:
        if isinstance(model, ModelArtifact):
            return model.artifact_id, model.artifact_size_bytes, model.runtime_profile
        return model.snapshot_id, model.expected_size_bytes, model.runtime_profile


def _safe_download_error(error: Exception) -> str:
    if isinstance(error, (ModelArtifactError, ModelSnapshotError)):
        return str(error)
    if isinstance(error, OSError):
        detail = error.strerror or "project storage operation failed"
        return f"{type(error).__name__}: {detail}"
    return "Download failed. Check network access and available project storage, then try again."


def load_model_artifact_catalog(path: Path) -> ModelArtifactCatalog:
    """Load the catalog and its pinned artifact manifests."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"unable to load model artifact catalog {path}: {error}"
        raise ModelArtifactError(msg) from error
    schema_version = _string(data, "schema_version")
    if schema_version != "heartwood.local-model-catalog.v1":
        msg = f"unsupported model artifact catalog schema: {schema_version}"
        raise ModelArtifactError(msg)
    models = data.get("models")
    if not isinstance(models, dict):
        msg = "model artifact catalog must include a models table"
        raise ModelArtifactError(msg)
    artifacts: list[ModelArtifact] = []
    for item in models.values():
        if not isinstance(item, dict) or "artifact_manifest" not in item:
            continue
        manifest = path.parents[3] / _string(item, "artifact_manifest")
        artifacts.append(_load_artifact(manifest))
    artifact_ids = [artifact.artifact_id for artifact in artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        msg = "model artifact ids must be unique"
        raise ModelArtifactError(msg)
    return ModelArtifactCatalog(schema_version=schema_version, artifacts=tuple(artifacts))


def download_model_artifact(
    artifact: ModelArtifact,
    *,
    cache_dir: Path,
    downloader: ArtifactDownloader | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Download one pinned artifact atomically and verify size and SHA-256."""
    artifact.validate()
    cache_root = cache_dir.resolve()
    destination = (cache_root / artifact.artifact_id).resolve()
    if cache_root != destination and cache_root not in destination.parents:
        msg = "model artifact cache path escapes configured cache directory"
        raise ModelArtifactError(msg)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(cache_root / f".{artifact.artifact_id}.lock", mode=0o600):
        installed = destination / artifact.source_path
        if destination.exists():
            try:
                _verify_artifact(installed, artifact)
            except (OSError, ValueError) as error:
                raise ModelArtifactError(
                    f"existing model artifact is incomplete or modified: {destination}: {error}"
                ) from error
            if progress_callback is not None:
                progress_callback(artifact.artifact_size_bytes, artifact.artifact_size_bytes)
            return installed
        available = shutil.disk_usage(cache_root).free
        if available < artifact.minimum_free_bytes:
            required_gib = artifact.minimum_free_bytes / (1024**3)
            available_gib = available / (1024**3)
            raise ModelArtifactError(
                f"artifact requires at least {required_gib:.1f} GiB free; "
                f"{available_gib:.1f} GiB is available under {cache_root}"
            )
        if downloader is None:
            downloader = cast(
                ArtifactDownloader,
                import_module("huggingface_hub").hf_hub_download,
            )
        staging = Path(tempfile.mkdtemp(prefix=f".{artifact.artifact_id}.", dir=cache_root))
        try:
            if progress_callback is None:
                downloaded_value = downloader(
                    repo_id=artifact.source_repository,
                    filename=artifact.source_path,
                    revision=artifact.source_revision,
                    local_dir=staging,
                )
            else:
                downloaded_value = downloader(
                    repo_id=artifact.source_repository,
                    filename=artifact.source_path,
                    revision=artifact.source_revision,
                    local_dir=staging,
                    tqdm_class=_progress_class(
                        progress_callback,
                        artifact.artifact_size_bytes,
                    ),
                )
            downloaded = Path(downloaded_value).resolve()
            expected = (staging / artifact.source_path).resolve()
            if downloaded.is_symlink() or not downloaded.is_file():
                raise ModelArtifactError(f"downloaded model artifact is missing: {downloaded}")
            if staging != downloaded and staging not in downloaded.parents:
                raise ModelArtifactError("downloaded model path escapes artifact staging directory")
            if downloaded != expected:
                raise ModelArtifactError("downloaded model path does not match its pinned source")
            _verify_artifact(downloaded, artifact)
            shutil.rmtree(staging / ".cache", ignore_errors=True)
            staging.replace(destination)
            installed = destination / artifact.source_path
            if progress_callback is not None:
                progress_callback(artifact.artifact_size_bytes, artifact.artifact_size_bytes)
            return installed
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _progress_class(
    callback: ProgressCallback,
    default_total: int,
) -> type[_DownloadProgress]:
    class BoundDownloadProgress(_DownloadProgress):
        _default_total = default_total

        @staticmethod
        def _report(downloaded: int, total: int) -> None:
            callback(downloaded, total)

    return BoundDownloadProgress


def _load_artifact(path: Path) -> ModelArtifact:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"unable to load model artifact manifest {path}: {error}"
        raise ModelArtifactError(msg) from error
    artifact = ModelArtifact(
        artifact_id=_string(data, "artifact_id"),
        runtime_profile=_string(data, "runtime_profile"),
        purpose=_string(data, "purpose"),
        source_repository=_string(data, "source_repository"),
        source_path=_string(data, "source_path"),
        source_revision=_string(data, "source_revision"),
        artifact_format=_string(data, "artifact_format"),
        artifact_size_bytes=_positive_int(data, "artifact_size_bytes"),
        minimum_free_bytes=_positive_int(data, "minimum_free_bytes"),
        artifact_sha256=_string(data, "artifact_sha256"),
        license_posture=_string(data, "license_posture"),
        model_alias=_string(data, "model_alias"),
        minimum_resource_envelope=_optional_string(data, "minimum_resource_envelope"),
        recommended_resource_envelope=_optional_string(data, "recommended_resource_envelope"),
        recommended=_optional_bool(data, "recommended", default=False),
    )
    artifact.validate()
    return artifact


def _verify_artifact(path: Path, artifact: ModelArtifact) -> None:
    verify_model_artifact(
        path,
        expected_size_bytes=artifact.artifact_size_bytes,
        expected_sha256=artifact.artifact_sha256,
    )


def verify_model_artifact(
    path: Path,
    *,
    expected_size_bytes: int,
    expected_sha256: str,
) -> None:
    """Verify one selected model file against its persisted integrity metadata."""
    if expected_size_bytes <= 0:
        raise ModelArtifactError("model artifact size must be positive")
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ModelArtifactError("model artifact checksum must be a lowercase SHA-256 digest")
    if path.is_symlink() or not path.is_file():
        msg = f"downloaded model artifact is missing: {path}"
        raise ModelArtifactError(msg)
    if path.stat().st_size != expected_size_bytes:
        msg = "downloaded model artifact size does not match its pinned manifest"
        raise ModelArtifactError(msg)
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        msg = "downloaded model artifact checksum does not match its pinned manifest"
        raise ModelArtifactError(msg)


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{key} must be a non-empty string"
        raise ModelArtifactError(msg)
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    return _string(data, key)


def _positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        msg = f"{key} must be a positive integer"
        raise ModelArtifactError(msg)
    return value


def _optional_bool(data: dict[str, Any], key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ModelArtifactError(f"{key} must be a boolean")
    return value
