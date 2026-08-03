# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic, verified transfer of local models into offline projects."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Self
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from heartwood.gateway._local_models import LocalModelChoice, ModelRepositoryError
from heartwood.gateway._model_artifacts import verify_model_artifact
from heartwood.gateway._model_snapshots import verify_model_snapshot
from heartwood.persistence import fsync_directory, native_file_lock

_BUNDLE_MANIFEST = "heartwood-model-bundle.json"
_BUNDLE_SCHEMA = "heartwood.model-bundle.v1"
_PAYLOAD_ROOT = PurePosixPath("model")
_CHUNK_SIZE = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_FILES = 100_000
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

type TransferKind = Literal["export", "import"]
type TransferStatus = Literal["cancelled", "cancelling", "error", "ready", "running"]
type TransferPhase = Literal[
    "preparing",
    "verifying",
    "exporting",
    "importing",
    "selecting",
    "complete",
]
type ProgressCallback = Callable[[int, int], None]
type ImportReadyCallback = Callable[[LocalModelChoice, Path, str], None]


class ModelTransferError(ValueError):
    """Raised when a model bundle or transfer operation is invalid."""


class ModelTransferCancelledError(ModelTransferError):
    """Raised internally after a cooperative transfer cancellation."""


class _BundleRecord(BaseModel):
    """Base for immutable, strict model-bundle records."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelBundleFile(_BundleRecord):
    """One regular payload file covered by the bundle manifest."""

    path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _path_is_clean(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("bundle file path must be a clean relative POSIX path")
        return path.as_posix()


class ModelBundleMetadata(_BundleRecord):
    """Portable model identity and runtime configuration."""

    model_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    runtime: Literal["llama-cpp", "vllm"]
    source_repository: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    source_path: str | None
    size_bytes: int = Field(gt=0)
    minimum_free_bytes: int = Field(gt=0)
    license_posture: str = Field(min_length=1)
    catalog_source: Literal["catalog", "user-selected", "transferred"]
    model_type: str | None = None
    context_window: int = Field(ge=2_048)
    artifact_sha256: str | None = None
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None
    license_id: str = Field(min_length=1)
    precision: str = Field(min_length=1)
    tier: Literal["standard", "powerful", "maximum"]
    qualification: Literal["unvalidated", "qualified"]
    minimum_gpu_count: int = Field(ge=0)
    minimum_gpu_memory_bytes: int = Field(ge=0)
    recommended_ram_bytes: int = Field(gt=0)
    recommended_disk_bytes: int = Field(gt=0)
    maximum_context_window: int = Field(ge=2_048)
    tool_call_parser: Literal["hermes", "openai", "qwen3_coder"] | None = None
    tensor_parallel_size: int = Field(gt=0)
    startup_seconds_min: int = Field(gt=0)
    startup_seconds_max: int = Field(gt=0)
    download_policy: str | None = None
    allow_patterns: tuple[str, ...] = ()
    ignore_patterns: tuple[str, ...] = ()
    validated_platforms: tuple[str, ...] = ()
    qualification_test: str | None = None
    qualification_date: str | None = None
    qualification_evidence: str | None = None
    recommended_cpu_count: int = Field(gt=0)

    def choice(self, *, transferred: bool = False) -> LocalModelChoice:
        """Return the normalized gateway model represented by this record."""
        values = self.model_dump(mode="python")
        if transferred:
            values["catalog_source"] = "transferred"
        choice = LocalModelChoice(**values)
        choice.validate()
        return choice


class ModelBundleManifest(_BundleRecord):
    """Canonical manifest stored in every portable Heartwood model bundle."""

    schema_version: Literal["heartwood.model-bundle.v1"] = "heartwood.model-bundle.v1"
    runtime_profile: Literal["llama-cpp-cpu", "vllm-cuda"]
    model: ModelBundleMetadata
    files: tuple[ModelBundleFile, ...] = Field(min_length=1, max_length=_MAX_FILES)
    total_size_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def _payload_matches_model(self) -> Self:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("bundle manifest contains duplicate file paths")
        if sum(file.size_bytes for file in self.files) != self.total_size_bytes:
            raise ValueError("bundle payload size does not match its file records")
        choice = self.model.choice()
        expected_profile = "llama-cpp-cpu" if choice.runtime == "llama-cpp" else "vllm-cuda"
        if self.runtime_profile != expected_profile:
            raise ValueError("bundle runtime profile does not match its model metadata")
        if choice.runtime == "llama-cpp":
            if choice.source_path is None or choice.artifact_sha256 is None:
                raise ValueError("GGUF bundle metadata is incomplete")
            if len(self.files) != 1 or self.files[0].path != choice.source_path:
                raise ValueError("GGUF bundles must contain exactly the selected model file")
            file = self.files[0]
            if file.size_bytes != choice.size_bytes or file.sha256 != choice.artifact_sha256:
                raise ValueError("GGUF bundle payload does not match its pinned artifact")
        else:
            names = set(paths)
            if "config.json" not in names or "SHA256SUMS" not in names:
                raise ValueError("vLLM bundles require config.json and SHA256SUMS")
            if not any(name.casefold().endswith(".safetensors") for name in names):
                raise ValueError("vLLM bundles require safetensors weights")
        return self

    def canonical_bytes(self) -> bytes:
        """Return deterministic manifest JSON used by reproducible exports."""
        payload = self.model_dump(mode="json")
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ModelTransferPlan:
    """Verified bundle metadata shown before an import begins."""

    bundle_path: Path
    bundle_size_bytes: int
    manifest: ModelBundleManifest

    @property
    def model(self) -> LocalModelChoice:
        """Return the normalized transferred model choice."""
        return self.manifest.model.choice(transferred=True)

    @property
    def manifest_sha256(self) -> str:
        """Return the stable identity of the exact manifest under review."""
        return _manifest_digest(self.manifest)


@dataclass(frozen=True, slots=True)
class ModelTransfer:
    """Content-safe status shared by terminal, notebook, and browser clients."""

    transfer_id: str
    kind: TransferKind
    status: TransferStatus
    phase: TransferPhase
    model_id: str
    label: str
    bytes_processed: int
    bytes_total: int
    bundle_path: str
    result_path: str | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def safe_dict(self) -> dict[str, object]:
        """Return the non-secret status representation used by public APIs."""
        return asdict(self)


class ModelTransferManager:
    """Run verified model exports and imports with shared progress and cancellation."""

    def __init__(self, *, models_dir: Path, on_import_ready: ImportReadyCallback) -> None:
        self.models_dir = models_dir
        self.on_import_ready = on_import_ready
        self._transfers: dict[str, ModelTransfer] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start_export(
        self,
        *,
        choice: LocalModelChoice,
        model_path: Path,
        bundle_path: Path,
        warnings: tuple[str, ...] = (),
    ) -> ModelTransfer:
        """Start or return one deterministic selected-model export."""
        choice.validate()
        output = _resolve_output_path(bundle_path)
        transfer_id = _transfer_id("export", output, choice.model_id)
        transfer = ModelTransfer(
            transfer_id=transfer_id,
            kind="export",
            status="running",
            phase="preparing",
            model_id=choice.model_id,
            label=choice.label,
            bytes_processed=0,
            bytes_total=choice.size_bytes,
            bundle_path=str(output),
            warnings=warnings,
        )
        return self._start(
            transfer,
            lambda cancel: self._export_worker(
                transfer_id,
                choice=choice,
                model_path=model_path,
                bundle_path=output,
                cancel=cancel,
            ),
        )

    def start_import(
        self,
        *,
        plan: ModelTransferPlan,
        approved: bool,
        warnings: tuple[str, ...] = (),
    ) -> ModelTransfer:
        """Start a verified bundle import after explicit license review."""
        if not approved:
            raise ModelTransferError(
                "model bundle import requires explicit approval of the displayed license record"
            )
        choice = plan.model
        transfer_id = _transfer_id("import", plan.bundle_path, _manifest_digest(plan.manifest))
        transfer = ModelTransfer(
            transfer_id=transfer_id,
            kind="import",
            status="running",
            phase="preparing",
            model_id=choice.model_id,
            label=choice.label,
            bytes_processed=0,
            bytes_total=plan.manifest.total_size_bytes,
            bundle_path=str(plan.bundle_path),
            warnings=warnings,
        )
        return self._start(
            transfer,
            lambda cancel: self._import_worker(transfer_id, plan=plan, cancel=cancel),
        )

    def statuses(self) -> tuple[ModelTransfer, ...]:
        """Return stable snapshots of current transfer operations."""
        with self._lock:
            return tuple(self._transfers.values())

    def status(self, transfer_id: str) -> ModelTransfer:
        """Return one transfer or reject an unknown identifier."""
        with self._lock:
            try:
                return self._transfers[transfer_id]
            except KeyError:
                raise ModelTransferError(f"unknown model transfer: {transfer_id}") from None

    def cancel(self, transfer_id: str) -> ModelTransfer:
        """Request cooperative cancellation of one active transfer."""
        with self._lock:
            try:
                current = self._transfers[transfer_id]
            except KeyError:
                raise ModelTransferError(f"unknown model transfer: {transfer_id}") from None
            if current.status != "running":
                return current
            self._cancellations[transfer_id].set()
            updated = replace(current, status="cancelling")
            self._transfers[transfer_id] = updated
            return updated

    def _start(
        self,
        transfer: ModelTransfer,
        operation: Callable[[threading.Event], None],
    ) -> ModelTransfer:
        with self._lock:
            current = self._transfers.get(transfer.transfer_id)
            if current is not None and current.status in {"running", "cancelling"}:
                return current
            cancel = threading.Event()
            self._transfers[transfer.transfer_id] = transfer
            self._cancellations[transfer.transfer_id] = cancel
        thread = threading.Thread(
            target=operation,
            args=(cancel,),
            daemon=True,
            name=f"heartwood-model-transfer-{transfer.transfer_id}",
        )
        thread.start()
        return transfer

    def _export_worker(
        self,
        transfer_id: str,
        *,
        choice: LocalModelChoice,
        model_path: Path,
        bundle_path: Path,
        cancel: threading.Event,
    ) -> None:
        try:
            self._phase(transfer_id, "verifying")
            result = export_model_bundle(
                choice,
                model_path=model_path,
                bundle_path=bundle_path,
                progress_callback=lambda processed, total: self._progress(
                    transfer_id, processed, total, phase="exporting"
                ),
                cancel=cancel,
            )
            self._ready(transfer_id, result)
        except ModelTransferCancelledError:
            self._cancelled(transfer_id)
        except Exception as error:
            self._failed(transfer_id, error)

    def _import_worker(
        self,
        transfer_id: str,
        *,
        plan: ModelTransferPlan,
        cancel: threading.Event,
    ) -> None:
        try:
            self._phase(transfer_id, "importing")
            choice, path, runtime_profile, created = import_model_bundle(
                plan,
                models_dir=self.models_dir,
                progress_callback=lambda processed, total: self._progress(
                    transfer_id, processed, total, phase="importing"
                ),
                cancel=cancel,
            )
            self._phase(transfer_id, "selecting")
            try:
                _raise_if_cancelled(cancel)
                self.on_import_ready(choice, path, runtime_profile)
            except Exception:
                if created:
                    shutil.rmtree(self.models_dir / choice.model_id, ignore_errors=True)
                    fsync_directory(self.models_dir)
                raise
            self._ready(transfer_id, path)
        except ModelTransferCancelledError:
            self._cancelled(transfer_id)
        except Exception as error:
            self._failed(transfer_id, error)

    def _phase(self, transfer_id: str, phase: TransferPhase) -> None:
        with self._lock:
            current = self._transfers[transfer_id]
            self._transfers[transfer_id] = replace(current, phase=phase)

    def _progress(
        self,
        transfer_id: str,
        processed: int,
        total: int,
        *,
        phase: TransferPhase,
    ) -> None:
        with self._lock:
            current = self._transfers[transfer_id]
            if current.status not in {"running", "cancelling"}:
                return
            self._transfers[transfer_id] = replace(
                current,
                phase=phase,
                bytes_processed=min(max(processed, current.bytes_processed, 0), total),
                bytes_total=total,
            )

    def _ready(self, transfer_id: str, result: Path) -> None:
        with self._lock:
            current = self._transfers[transfer_id]
            self._transfers[transfer_id] = replace(
                current,
                status="ready",
                phase="complete",
                bytes_processed=current.bytes_total,
                result_path=str(result),
            )

    def _cancelled(self, transfer_id: str) -> None:
        with self._lock:
            current = self._transfers[transfer_id]
            self._transfers[transfer_id] = replace(
                current,
                status="cancelled",
                error=None,
            )

    def _failed(self, transfer_id: str, error: Exception) -> None:
        with self._lock:
            current = self._transfers[transfer_id]
            self._transfers[transfer_id] = replace(
                current,
                status="error",
                error=_safe_transfer_error(error),
            )


def inspect_model_bundle(path: Path) -> ModelTransferPlan:
    """Validate bundle structure and return its content-safe import plan."""
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ModelTransferError(f"model bundle must not be a symbolic link: {candidate}")
    bundle = candidate.resolve()
    if not bundle.is_file():
        raise ModelTransferError(f"model bundle must be a regular file: {bundle}")
    try:
        with ZipFile(bundle, "r") as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_FILES + 1:
                raise ModelTransferError("model bundle contains too many files")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ModelTransferError("model bundle contains duplicate archive paths")
            manifest_info = _member(infos, _BUNDLE_MANIFEST)
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise ModelTransferError("model bundle manifest exceeds the supported size")
            _validate_zip_member(manifest_info)
            try:
                manifest = ModelBundleManifest.model_validate_json(
                    archive.read(manifest_info),
                    strict=True,
                )
            except ValidationError as error:
                raise ModelTransferError(f"model bundle manifest is invalid: {error}") from error
            expected = {
                _BUNDLE_MANIFEST,
                *(_payload_archive_path(file.path) for file in manifest.files),
            }
            if set(names) != expected:
                raise ModelTransferError("model bundle contents do not match its manifest")
            records = {file.path: file for file in manifest.files}
            for info in infos:
                _validate_zip_member(info)
                if info.filename == _BUNDLE_MANIFEST:
                    continue
                relative = _payload_relative_path(info.filename)
                record = records[relative]
                if info.file_size != record.size_bytes:
                    raise ModelTransferError(
                        f"model bundle file size does not match its manifest: {relative}"
                    )
    except BadZipFile as error:
        raise ModelTransferError(f"model bundle is not a valid ZIP64 archive: {bundle}") from error
    return ModelTransferPlan(
        bundle_path=bundle,
        bundle_size_bytes=bundle.stat().st_size,
        manifest=manifest,
    )


def export_model_bundle(
    choice: LocalModelChoice,
    *,
    model_path: Path,
    bundle_path: Path,
    progress_callback: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Verify and export one selected model as a reproducible ZIP64 bundle."""
    choice.validate()
    output = _resolve_output_path(bundle_path)
    if output.exists() or output.is_symlink():
        raise ModelTransferError(f"model bundle output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    files = _verified_source_files(choice, model_path, cancel=cancel)
    manifest = ModelBundleManifest(
        runtime_profile="llama-cpp-cpu" if choice.runtime == "llama-cpp" else "vllm-cuda",
        model=ModelBundleMetadata.model_validate(choice.safe_dict(), strict=True),
        files=tuple(record for record, _path in files),
        total_size_bytes=sum(record.size_bytes for record, _path in files),
    )
    temporary = output.with_name(f".{output.name}.heartwood-partial")
    lock_path = output.with_name(f".{output.name}.heartwood-lock")
    with native_file_lock(lock_path, secure_parent=False):
        if output.exists() or output.is_symlink():
            raise ModelTransferError(f"model bundle output already exists: {output}")
        _remove_regular_temporary(temporary)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            with os.fdopen(descriptor, "w+b") as file:
                descriptor = -1
                with ZipFile(file, "w", compression=ZIP_STORED, allowZip64=True) as archive:
                    archive.writestr(
                        _zip_info(_BUNDLE_MANIFEST, len(manifest.canonical_bytes())),
                        manifest.canonical_bytes(),
                    )
                    processed = 0
                    for record, source in files:
                        _raise_if_cancelled(cancel)
                        digest = hashlib.sha256()
                        info = _zip_info(_payload_archive_path(record.path), record.size_bytes)
                        with (
                            source.open("rb") as input_file,
                            archive.open(
                                info,
                                "w",
                                force_zip64=True,
                            ) as output_file,
                        ):
                            while chunk := input_file.read(_CHUNK_SIZE):
                                _raise_if_cancelled(cancel)
                                output_file.write(chunk)
                                digest.update(chunk)
                                processed += len(chunk)
                                if progress_callback is not None:
                                    progress_callback(processed, manifest.total_size_bytes)
                        if digest.hexdigest() != record.sha256:
                            raise ModelTransferError(
                                f"model file changed while the bundle was exported: {record.path}"
                            )
                file.flush()
                os.fsync(file.fileno())
            _raise_if_cancelled(cancel)
            _publish_new_file(temporary, output)
            fsync_directory(output.parent)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
    return output


def import_model_bundle(
    plan: ModelTransferPlan,
    *,
    models_dir: Path,
    progress_callback: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> tuple[LocalModelChoice, Path, str, bool]:
    """Verify and atomically import one inspected model bundle."""
    current = inspect_model_bundle(plan.bundle_path)
    if current.manifest != plan.manifest:
        raise ModelTransferError("model bundle changed after it was reviewed")
    manifest = current.manifest
    choice = manifest.model.choice(transferred=True)
    models_root = models_dir.expanduser().resolve()
    models_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = (models_root / choice.model_id).resolve()
    if models_root not in destination.parents:
        raise ModelTransferError("model bundle destination escapes project model storage")
    staging = models_root / f".{choice.model_id}.import-partial"
    lock_path = models_root / f".{choice.model_id}.lock"
    required = manifest.total_size_bytes + 512 * 1024 * 1024
    with native_file_lock(lock_path):
        if destination.exists() or destination.is_symlink():
            _verify_imported_payload(
                destination,
                manifest,
                verify_transfer_digests=True,
                cancel=cancel,
            )
            selected = _selected_path(destination, choice)
            return choice, selected, manifest.runtime_profile, False
        available = shutil.disk_usage(models_root).free
        if available < required:
            raise ModelTransferError(
                "insufficient project storage for the model import and runtime reserve: "
                f"need {required} bytes, found {available}"
            )
        _remove_staging_directory(staging)
        staging.mkdir(mode=0o700)
        try:
            processed = 0
            records = {file.path: file for file in manifest.files}
            with ZipFile(current.bundle_path, "r") as archive:
                for relative, record in sorted(records.items()):
                    _raise_if_cancelled(cancel)
                    info = archive.getinfo(_payload_archive_path(relative))
                    target = _safe_staging_target(staging, relative)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    descriptor = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    digest = hashlib.sha256()
                    try:
                        with (
                            archive.open(info, "r") as input_file,
                            os.fdopen(descriptor, "wb") as output_file,
                        ):
                            descriptor = -1
                            while chunk := input_file.read(_CHUNK_SIZE):
                                _raise_if_cancelled(cancel)
                                output_file.write(chunk)
                                digest.update(chunk)
                                processed += len(chunk)
                                if progress_callback is not None:
                                    progress_callback(processed, manifest.total_size_bytes)
                            output_file.flush()
                            os.fsync(output_file.fileno())
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    if digest.hexdigest() != record.sha256:
                        raise ModelTransferError(f"model bundle checksum mismatch: {relative}")
            _raise_if_cancelled(cancel)
            _verify_imported_payload(
                staging,
                manifest,
                verify_transfer_digests=False,
                cancel=cancel,
            )
            _raise_if_cancelled(cancel)
            fsync_directory(staging)
            staging.replace(destination)
            fsync_directory(models_root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return choice, _selected_path(destination, choice), manifest.runtime_profile, True


def _verified_source_files(
    choice: LocalModelChoice,
    model_path: Path,
    *,
    cancel: threading.Event | None = None,
) -> tuple[tuple[ModelBundleFile, Path], ...]:
    candidate = model_path.expanduser()
    if candidate.is_symlink():
        raise ModelTransferError(f"selected model must not be a symbolic link: {candidate}")
    selected = candidate.resolve()
    if choice.runtime == "llama-cpp":
        if choice.source_path is None or choice.artifact_sha256 is None:
            raise ModelTransferError("selected GGUF model metadata is incomplete")
        verify_model_artifact(
            selected,
            expected_size_bytes=choice.size_bytes,
            expected_sha256=choice.artifact_sha256,
            checkpoint=lambda: _raise_if_cancelled(cancel),
        )
        return (
            (
                ModelBundleFile(
                    path=choice.source_path,
                    size_bytes=selected.stat().st_size,
                    sha256=choice.artifact_sha256,
                ),
                selected,
            ),
        )
    try:
        verified_digests = verify_model_snapshot(
            selected,
            checkpoint=lambda: _raise_if_cancelled(cancel),
        )
    except ValueError as error:
        raise ModelTransferError(f"selected model snapshot is invalid: {error}") from error
    files: list[tuple[ModelBundleFile, Path]] = []
    for path in sorted(selected.rglob("*")):
        if path.is_symlink():
            raise ModelTransferError(f"model snapshot contains a symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(selected).as_posix()
        files.append(
            (
                ModelBundleFile(
                    path=relative,
                    size_bytes=path.stat().st_size,
                    sha256=(
                        _sha256(path, cancel=cancel)
                        if relative == "SHA256SUMS"
                        else verified_digests[relative]
                    ),
                ),
                path,
            )
        )
    return tuple(files)


def _verify_imported_payload(
    root: Path,
    manifest: ModelBundleManifest,
    *,
    verify_transfer_digests: bool,
    cancel: threading.Event | None,
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ModelTransferError("imported model destination must be a regular directory")
    expected = {file.path: file for file in manifest.files}
    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ModelTransferError(f"imported model contains a symbolic link: {relative}")
        if path.is_file():
            actual.add(relative)
    if actual != set(expected):
        raise ModelTransferError("imported model files do not match the transfer manifest")
    if verify_transfer_digests:
        for relative, record in expected.items():
            _raise_if_cancelled(cancel)
            path = root.joinpath(*PurePosixPath(relative).parts)
            if (
                path.stat().st_size != record.size_bytes
                or _sha256(
                    path,
                    cancel=cancel,
                )
                != record.sha256
            ):
                raise ModelTransferError(f"imported model checksum mismatch: {relative}")
    choice = manifest.model.choice(transferred=True)
    if choice.runtime == "llama-cpp":
        if choice.source_path is None or choice.artifact_sha256 is None:
            raise ModelTransferError("imported GGUF metadata is incomplete")
        verify_model_artifact(
            root / choice.source_path,
            expected_size_bytes=choice.size_bytes,
            expected_sha256=choice.artifact_sha256,
            checkpoint=lambda: _raise_if_cancelled(cancel),
        )
        return
    try:
        verify_model_snapshot(root, checkpoint=lambda: _raise_if_cancelled(cancel))
    except ValueError as error:
        raise ModelTransferError(f"imported model snapshot is invalid: {error}") from error


def _selected_path(destination: Path, choice: LocalModelChoice) -> Path:
    if choice.runtime == "vllm":
        return destination
    if choice.source_path is None:  # pragma: no cover - choice invariant
        raise ModelTransferError("imported GGUF source path is unavailable")
    return destination.joinpath(*PurePosixPath(choice.source_path).parts)


def _resolve_output_path(path: Path) -> Path:
    expanded = path.expanduser()
    parent = expanded.parent.resolve()
    if not parent.is_dir():
        raise ModelTransferError(f"model bundle output directory does not exist: {parent}")
    return parent / expanded.name


def _safe_staging_target(staging: Path, relative: str) -> Path:
    target = staging.joinpath(*PurePosixPath(relative).parts).resolve()
    if staging != target and staging not in target.parents:
        raise ModelTransferError(f"model bundle path escapes import staging: {relative}")
    return target


def _validate_zip_member(info: ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ModelTransferError(f"model bundle contains an unsafe archive path: {info.filename}")
    if info.is_dir() or info.compress_type != ZIP_STORED or info.flag_bits & 0x1:
        raise ModelTransferError(f"model bundle contains an unsupported member: {info.filename}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if info.create_system == 3 and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
        raise ModelTransferError(f"model bundle member is not a regular file: {info.filename}")


def _member(infos: Iterable[ZipInfo], name: str) -> ZipInfo:
    for info in infos:
        if info.filename == name:
            return info
    raise ModelTransferError(f"model bundle is missing {name}")


def _payload_archive_path(relative: str) -> str:
    return (_PAYLOAD_ROOT / PurePosixPath(relative)).as_posix()


def _payload_relative_path(archive_path: str) -> str:
    path = PurePosixPath(archive_path)
    if path.parts[:1] != (_PAYLOAD_ROOT.name,) or len(path.parts) < 2:
        raise ModelTransferError(f"model bundle contains an unexpected path: {archive_path}")
    return PurePosixPath(*path.parts[1:]).as_posix()


def _zip_info(name: str, size: int) -> ZipInfo:
    info = ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.file_size = size
    return info


def _sha256(path: Path, *, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as file:
        while chunk := file.read(_CHUNK_SIZE):
            _raise_if_cancelled(cancel)
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_digest(manifest: ModelBundleManifest) -> str:
    return hashlib.sha256(manifest.canonical_bytes()).hexdigest()


def _transfer_id(kind: TransferKind, path: Path, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}:{path}:{identity}".encode()).hexdigest()[:20]
    return f"{kind}-{digest}"


def _remove_regular_temporary(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ModelTransferError(f"model bundle temporary path is unsafe: {path}")
    path.unlink(missing_ok=True)


def _publish_new_file(temporary: Path, output: Path) -> None:
    """Atomically publish a new file without replacing an existing destination."""
    try:
        os.link(temporary, output, follow_symlinks=False)
    except FileExistsError as error:
        raise ModelTransferError(f"model bundle output already exists: {output}") from error
    except OSError as error:
        raise ModelTransferError(
            "model bundle output storage does not support atomic publication"
        ) from error
    try:
        temporary.unlink()
    except OSError:
        output.unlink(missing_ok=True)
        raise


def _remove_staging_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ModelTransferError(f"model import staging path is unsafe: {path}")
    shutil.rmtree(path, ignore_errors=True)


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ModelTransferCancelledError("model transfer was cancelled")


def _safe_transfer_error(error: Exception) -> str:
    if isinstance(error, (ModelRepositoryError, ModelTransferError)):
        return str(error)
    if isinstance(error, OSError):
        return f"{type(error).__name__}: {error.strerror or 'project storage operation failed'}"
    return "Model transfer failed. Verify the bundle and available project storage, then retry."


__all__ = [
    "ModelBundleFile",
    "ModelBundleManifest",
    "ModelBundleMetadata",
    "ModelTransfer",
    "ModelTransferCancelledError",
    "ModelTransferError",
    "ModelTransferManager",
    "ModelTransferPlan",
    "export_model_bundle",
    "import_model_bundle",
    "inspect_model_bundle",
]
