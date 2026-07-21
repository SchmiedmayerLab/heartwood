# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reviewed import of existing model artifacts into Heartwood project storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from heartwood.gateway._local_model_contract import (
    DEFAULT_LOCAL_CONTEXT_WINDOW,
    MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
)
from heartwood.gateway._local_models import (
    LocalModelChoice,
    LocalModelRuntime,
    ModelRepositoryError,
    infer_model_type,
)
from heartwood.gateway._model_identity import (
    is_hugging_face_model_id,
    is_resolved_revision,
)
from heartwood.gateway._model_snapshots import (
    verify_model_snapshot,
    write_model_snapshot_manifest,
)

_IMPORT_RESERVE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LocalModelImport:
    """One imported model selection and its project-local runtime path."""

    model: LocalModelChoice
    path: Path

    @property
    def storage_root(self) -> Path:
        """Return the project directory committed for this imported model."""
        return self.path if self.path.is_dir() else self.path.parent

    def safe_dict(self) -> dict[str, object]:
        """Return import metadata without the original filesystem path."""
        return {
            "model": self.model.safe_dict(),
            "path": str(self.path),
            "status": "ready",
        }


def import_local_model(
    source: Path,
    *,
    models_dir: Path,
    source_repository: str,
    source_revision: str,
    license_posture: str,
    context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW,
) -> LocalModelImport:
    """Copy a supported GGUF file or vLLM snapshot without following links."""
    if not is_hugging_face_model_id(source_repository):
        raise ModelRepositoryError("source repository must use the Hugging Face owner/model form")
    if not is_resolved_revision(source_revision):
        raise ModelRepositoryError("source revision must be an immutable commit hash")
    if not license_posture.strip():
        raise ModelRepositoryError("the upstream model license must be recorded")
    if context_window < MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:
        raise ModelRepositoryError(
            "Heartwood agent sessions require an imported model context window of at least "
            f"{MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:,} tokens"
        )
    source = source.expanduser()
    if not source.exists():
        raise ModelRepositoryError(f"model source does not exist: {source}")
    _reject_symlinks(source)
    source = source.resolve()
    models_dir = models_dir.expanduser().resolve()
    if (
        source == models_dir
        or source.is_relative_to(models_dir)
        or models_dir.is_relative_to(source)
    ):
        raise ModelRepositoryError(
            "model source and Heartwood project model storage must be separate paths"
        )
    runtime = _runtime_for_source(source)
    model_type = infer_model_type(
        source_repository,
        _declared_model_type(source) if runtime == "vllm" else None,
    )
    size_bytes = _source_size(source)
    minimum_free_bytes = size_bytes + _IMPORT_RESERVE_BYTES
    available = shutil.disk_usage(models_dir).free
    if available < minimum_free_bytes:
        raise ModelRepositoryError(
            "insufficient project storage for the model copy and runtime reserve: "
            f"need {minimum_free_bytes} bytes, found {available}"
        )
    identity = hashlib.sha256(
        f"{source_repository}@{source_revision}:{source.name}:{runtime}".encode()
    ).hexdigest()[:16]
    model_id = f"imported-{identity}"
    destination = models_dir / model_id
    if destination.exists() or destination.is_symlink():
        raise ModelRepositoryError(f"this model is already imported: {model_id}")
    checksum = _sha256(source) if runtime == "llama-cpp" else None
    choice = LocalModelChoice(
        model_id=model_id,
        label=source_repository.rsplit("/", maxsplit=1)[-1],
        purpose=(
            "User-imported Hugging Face model; Heartwood has not reviewed its capabilities, "
            "license, or suitability."
        ),
        runtime=runtime,
        source_repository=source_repository,
        source_revision=source_revision,
        source_path=source.name if runtime == "llama-cpp" else None,
        size_bytes=size_bytes,
        minimum_free_bytes=minimum_free_bytes,
        license_posture=license_posture.strip(),
        catalog_source="user-selected",
        model_type=model_type,
        context_window=context_window,
        artifact_sha256=checksum,
        minimum_resource_envelope=_minimum_resource_envelope(runtime, size_bytes),
        recommended_resource_envelope=_recommended_resource_envelope(runtime, size_bytes),
    )
    choice.validate()
    temporary = Path(tempfile.mkdtemp(prefix=f".{model_id}.", dir=models_dir))
    try:
        if source.is_file():
            imported_path = temporary / source.name
            shutil.copy2(source, imported_path, follow_symlinks=False)
        else:
            _copy_directory(source, temporary)
        provenance = {
            "schema_version": "heartwood.local-model-import.v1",
            "source_repository": source_repository,
            "source_revision": source_revision,
            "source_path": source.name if runtime == "llama-cpp" else None,
            "license_posture": license_posture.strip(),
            "size_bytes": size_bytes,
            "runtime": runtime,
            "model_type": model_type,
            "artifact_sha256": checksum,
        }
        (temporary / "heartwood-model.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if runtime == "vllm":
            write_model_snapshot_manifest(temporary)
            verify_model_snapshot(temporary)
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    selected_path = destination / source.name if runtime == "llama-cpp" else destination
    return LocalModelImport(model=choice, path=selected_path)


def _runtime_for_source(source: Path) -> LocalModelRuntime:
    if source.is_file():
        if source.suffix.casefold() != ".gguf":
            raise ModelRepositoryError("a managed model file must use the GGUF format")
        with source.open("rb") as file:
            if file.read(4) != b"GGUF":
                raise ModelRepositoryError("the selected file does not contain a GGUF header")
        return "llama-cpp"
    if not source.is_dir():
        raise ModelRepositoryError("model source must be a regular file or directory")
    config_path = source / "config.json"
    if not config_path.is_file():
        raise ModelRepositoryError("a vLLM model directory must contain config.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelRepositoryError("the vLLM model config.json is invalid") from error
    if not isinstance(config, dict) or not isinstance(config.get("architectures"), list):
        raise ModelRepositoryError("the vLLM model must declare its architecture")
    if config.get("auto_map"):
        raise ModelRepositoryError("models that require custom remote code are not supported")
    if any(path.suffix.casefold() == ".py" for path in source.rglob("*")):
        raise ModelRepositoryError("model snapshots containing executable Python are not supported")
    has_weights = any(path.name.casefold().endswith(".safetensors") for path in source.rglob("*"))
    if not has_weights:
        raise ModelRepositoryError("a vLLM model directory must contain safetensors weights")
    return "vllm"


def _declared_model_type(source: Path) -> object:
    """Return model-family metadata from a directory already validated above."""
    try:
        config = json.loads((source / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - validated by _runtime_for_source
        return None
    return config.get("model_type") if isinstance(config, dict) else None


def _reject_symlinks(source: Path) -> None:
    if source.is_symlink():
        raise ModelRepositoryError("model imports do not follow symbolic links")
    if source.is_dir() and any(path.is_symlink() for path in source.rglob("*")):
        raise ModelRepositoryError("model directories must not contain symbolic links")


def _source_size(source: Path) -> int:
    if source.is_file():
        return source.stat().st_size
    return sum(path.stat().st_size for path in source.rglob("*") if path.is_file())


def _copy_directory(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(mode=0o700, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(item, target, follow_symlinks=False)
        else:
            raise ModelRepositoryError("model snapshots may contain only regular files")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _minimum_resource_envelope(runtime: str, size_bytes: int) -> str:
    gib = max(1, (size_bytes + (1024**3 - 1)) // (1024**3))
    if runtime == "vllm":
        return f"NVIDIA GPU with at least {gib + 2} GiB VRAM"
    return f"At least {gib + 2} GiB available system memory"


def _recommended_resource_envelope(runtime: str, size_bytes: int) -> str:
    gib = max(1, (size_bytes + (1024**3 - 1)) // (1024**3))
    if runtime == "vllm":
        return f"NVIDIA GPU with at least {gib + 6} GiB VRAM"
    return f"At least {gib + 6} GiB available system memory"
