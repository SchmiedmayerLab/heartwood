# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reviewed local-model artifacts downloaded to mounted runtime storage."""

from __future__ import annotations

import hashlib
import threading
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, TypeAlias, cast

DownloadStatus: TypeAlias = Literal["downloading", "error", "ready"]
ProgressCallback: TypeAlias = Callable[[int, int], None]


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
    artifact_sha256: str
    license_posture: str
    model_alias: str
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None

    def validate(self) -> None:
        """Validate pinned identity and integrity metadata."""
        if not self.artifact_id or "/" in self.artifact_id or ".." in self.artifact_id:
            msg = "artifact_id must be a safe cache directory name"
            raise ModelArtifactError(msg)
        if not self.source_repository or "/" not in self.source_repository:
            msg = "source_repository must be a Hugging Face owner/repository id"
            raise ModelArtifactError(msg)
        if (
            not self.source_path
            or Path(self.source_path).is_absolute()
            or ".." in Path(self.source_path).parts
        ):
            msg = "source_path must be a safe repository-relative path"
            raise ModelArtifactError(msg)
        if not self.source_revision or self.source_revision in {"main", "master", "latest"}:
            msg = "source_revision must be an immutable revision"
            raise ModelArtifactError(msg)
        if self.artifact_size_bytes <= 0:
            msg = "artifact_size_bytes must be positive"
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
    """Reviewed downloadable artifacts keyed by stable id."""

    schema_version: str
    artifacts: tuple[ModelArtifact, ...]

    def artifact(self, artifact_id: str) -> ModelArtifact:
        """Return one reviewed artifact."""
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

    artifact_id: str
    status: DownloadStatus
    bytes_downloaded: int
    bytes_total: int
    path: str | None = None
    error: str | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return serializable download status."""
        return asdict(self)


class ModelArtifactManager:
    """Download reviewed artifacts in background for the web settings panel."""

    def __init__(self, *, catalog: ModelArtifactCatalog, cache_dir: Path) -> None:
        self.catalog = catalog
        self.cache_dir = cache_dir
        self._downloads: dict[str, ModelDownload] = {}
        self._lock = threading.Lock()

    def start(self, artifact_id: str) -> ModelDownload:
        """Start or return the current download for an artifact."""
        artifact = self.catalog.artifact(artifact_id)
        with self._lock:
            current = self._downloads.get(artifact_id)
            if current is not None and current.status in {"downloading", "ready"}:
                return current
            download = ModelDownload(
                artifact_id=artifact_id,
                status="downloading",
                bytes_downloaded=0,
                bytes_total=artifact.artifact_size_bytes,
            )
            self._downloads[artifact_id] = download
        thread = threading.Thread(
            target=self._download,
            args=(artifact,),
            daemon=True,
            name=f"heartwood-model-{artifact_id}",
        )
        thread.start()
        return download

    def statuses(self) -> tuple[ModelDownload, ...]:
        """Return stable snapshots of current downloads."""
        with self._lock:
            return tuple(self._downloads.values())

    def _download(self, artifact: ModelArtifact) -> None:
        try:
            path = download_model_artifact(
                artifact,
                cache_dir=self.cache_dir,
                progress_callback=lambda downloaded, _total: self._record_progress(
                    artifact, downloaded
                ),
            )
            result = ModelDownload(
                artifact_id=artifact.artifact_id,
                status="ready",
                bytes_downloaded=artifact.artifact_size_bytes,
                bytes_total=artifact.artifact_size_bytes,
                path=str(path),
            )
        except Exception as error:  # pragma: no cover - network failures vary by environment
            with self._lock:
                downloaded = self._downloads[artifact.artifact_id].bytes_downloaded
                self._downloads[artifact.artifact_id] = ModelDownload(
                    artifact_id=artifact.artifact_id,
                    status="error",
                    bytes_downloaded=downloaded,
                    bytes_total=artifact.artifact_size_bytes,
                    error=f"{type(error).__name__}: artifact download failed",
                )
            return
        with self._lock:
            self._downloads[artifact.artifact_id] = result

    def _record_progress(self, artifact: ModelArtifact, downloaded: int) -> None:
        with self._lock:
            current = self._downloads.get(artifact.artifact_id)
            if current is None or current.status != "downloading":
                return
            self._downloads[artifact.artifact_id] = ModelDownload(
                artifact_id=artifact.artifact_id,
                status="downloading",
                bytes_downloaded=min(max(downloaded, 0), artifact.artifact_size_bytes),
                bytes_total=artifact.artifact_size_bytes,
            )


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
    """Download one pinned artifact and verify size and SHA-256."""
    artifact.validate()
    destination = (cache_dir / artifact.artifact_id).resolve()
    cache_root = cache_dir.resolve()
    if cache_root != destination and cache_root not in destination.parents:
        msg = "model artifact cache path escapes configured cache directory"
        raise ModelArtifactError(msg)
    destination.mkdir(parents=True, exist_ok=True)
    if downloader is None:
        downloader = cast(
            ArtifactDownloader,
            import_module("huggingface_hub").hf_hub_download,
        )
    if progress_callback is None:
        downloaded_value = downloader(
            repo_id=artifact.source_repository,
            filename=artifact.source_path,
            revision=artifact.source_revision,
            local_dir=destination,
        )
    else:
        downloaded_value = downloader(
            repo_id=artifact.source_repository,
            filename=artifact.source_path,
            revision=artifact.source_revision,
            local_dir=destination,
            tqdm_class=_progress_class(progress_callback, artifact.artifact_size_bytes),
        )
    downloaded = Path(downloaded_value).resolve()
    if destination != downloaded and destination not in downloaded.parents:
        msg = "downloaded model path escapes artifact cache directory"
        raise ModelArtifactError(msg)
    _verify_artifact(downloaded, artifact)
    if progress_callback is not None:
        progress_callback(artifact.artifact_size_bytes, artifact.artifact_size_bytes)
    return downloaded


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
        artifact_sha256=_string(data, "artifact_sha256"),
        license_posture=_string(data, "license_posture"),
        model_alias=_string(data, "model_alias"),
        minimum_resource_envelope=_optional_string(data, "minimum_resource_envelope"),
        recommended_resource_envelope=_optional_string(data, "recommended_resource_envelope"),
    )
    artifact.validate()
    return artifact


def _verify_artifact(path: Path, artifact: ModelArtifact) -> None:
    if not path.is_file():
        msg = f"downloaded model artifact is missing: {path}"
        raise ModelArtifactError(msg)
    if path.stat().st_size != artifact.artifact_size_bytes:
        msg = "downloaded model artifact size does not match its reviewed manifest"
        raise ModelArtifactError(msg)
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != artifact.artifact_sha256:
        msg = "downloaded model artifact checksum does not match its reviewed manifest"
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
