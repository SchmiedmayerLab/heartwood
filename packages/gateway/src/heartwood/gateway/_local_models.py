# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Normalized local-model choices and Hugging Face repository inspection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import PurePosixPath
from typing import Literal, Protocol, cast

from heartwood.gateway._model_artifacts import ModelArtifact
from heartwood.gateway._model_snapshots import ModelSnapshot

type LocalModelRuntime = Literal["llama-cpp", "vllm"]
type LocalModelCatalogSource = Literal["recommended", "user-selected"]

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_PINNED_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
_RESOLVED_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_SPLIT_GGUF = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)
_SAFETENSORS_WEIGHTS = re.compile(
    r"^model(?:-\d{5}-of-\d{5})?\.safetensors(?:\.index\.json)?$",
    re.IGNORECASE,
)
_PYTORCH_WEIGHTS = re.compile(
    r"^pytorch_model(?:-\d{5}-of-\d{5})?\.bin(?:\.index\.json)?$",
    re.IGNORECASE,
)
_ISSUE_URL = "https://github.com/SchmiedmayerLab/heartwood/issues/new/choose"
_GGUF_PREFERENCE = ("q4_k_m", "q5_k_m", "q4_k_s", "q5_k_s", "q8_0")
_USER_SELECTED_PURPOSE = (
    "User-selected Hugging Face model; Heartwood has not reviewed its capabilities, "
    "license, or suitability."
)


class ModelRepositoryError(ValueError):
    """Raised when a model repository cannot produce a supported local candidate."""


class ModelInfoProvider(Protocol):
    """Subset of ``huggingface_hub.HfApi.model_info`` used by the gateway."""

    def __call__(
        self,
        repo_id: str,
        *,
        revision: str | None = None,
        files_metadata: bool = False,
        token: str | bool | None = None,
    ) -> object:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LocalModelChoice:
    """One gateway-normalized model that Heartwood can download and launch."""

    model_id: str
    label: str
    purpose: str
    runtime: LocalModelRuntime
    source_repository: str
    source_revision: str
    source_path: str | None
    size_bytes: int
    minimum_free_bytes: int
    license_posture: str
    catalog_source: LocalModelCatalogSource
    artifact_sha256: str | None = None
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None

    def validate(self) -> None:
        """Validate source provenance and runtime-specific integrity metadata."""
        if not self.model_id or "/" in self.model_id or ".." in self.model_id:
            raise ModelRepositoryError("local model id must be a safe directory name")
        if not self.label.strip() or not self.purpose.strip():
            raise ModelRepositoryError("local model label and purpose must not be empty")
        if _REPOSITORY.fullmatch(self.source_repository) is None:
            raise ModelRepositoryError("repository must be a Hugging Face owner/model id")
        revision_pattern = (
            _RESOLVED_REVISION if self.catalog_source == "user-selected" else _PINNED_REVISION
        )
        if revision_pattern.fullmatch(self.source_revision) is None:
            raise ModelRepositoryError("model revision must resolve to an immutable commit")
        if self.size_bytes <= 0 or self.minimum_free_bytes < self.size_bytes:
            raise ModelRepositoryError("local model storage metadata is invalid")
        if not self.license_posture.strip():
            raise ModelRepositoryError("local model license posture must not be empty")
        if self.runtime == "llama-cpp":
            if self.source_path is None or not self.source_path.casefold().endswith(".gguf"):
                raise ModelRepositoryError("CPU models require one GGUF file")
            source_path = PurePosixPath(self.source_path)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise ModelRepositoryError("model file must be a safe repository-relative path")
            if (
                self.artifact_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256) is None
            ):
                raise ModelRepositoryError("GGUF models require a source SHA-256 digest")
        elif self.source_path is not None or self.artifact_sha256 is not None:
            raise ModelRepositoryError("GPU snapshots must not select one repository file")

    def safe_dict(self) -> dict[str, object]:
        """Return non-secret model metadata for every interaction surface."""
        self.validate()
        return asdict(self)

    def download_model(self) -> ModelArtifact | ModelSnapshot:
        """Translate the normalized choice to the existing download implementation."""
        self.validate()
        runtime_profile = "llama-cpp-cpu" if self.runtime == "llama-cpp" else "vllm-cuda"
        if self.runtime == "llama-cpp":
            if self.source_path is None or self.artifact_sha256 is None:  # pragma: no cover
                raise ModelRepositoryError("CPU model metadata is incomplete")
            return ModelArtifact(
                artifact_id=self.model_id,
                runtime_profile=runtime_profile,
                purpose=self.purpose,
                source_repository=self.source_repository,
                source_path=self.source_path,
                source_revision=self.source_revision,
                artifact_format="GGUF",
                artifact_size_bytes=self.size_bytes,
                minimum_free_bytes=self.minimum_free_bytes,
                artifact_sha256=self.artifact_sha256,
                license_posture=self.license_posture,
                model_alias=self.label,
                minimum_resource_envelope=self.minimum_resource_envelope,
                recommended_resource_envelope=self.recommended_resource_envelope,
                recommended=False,
            )
        return ModelSnapshot(
            snapshot_id=self.model_id,
            runtime_profile=runtime_profile,
            purpose=self.purpose,
            source_repository=self.source_repository,
            source_revision=self.source_revision,
            expected_size_bytes=self.size_bytes,
            minimum_free_bytes=self.minimum_free_bytes,
            license_posture=self.license_posture,
            model_alias=self.label,
            minimum_resource_envelope=self.minimum_resource_envelope,
            recommended_resource_envelope=self.recommended_resource_envelope,
            recommended=False,
        )


@dataclass(frozen=True, slots=True)
class LocalModelDownloadPlan:
    """One automatic local-model choice for the current deployment."""

    model: LocalModelChoice
    selection_reason: str

    def safe_dict(self) -> dict[str, object]:
        """Return the exact model, runtime, and resource plan shown before download."""
        return {
            "model": self.model.safe_dict(),
            "selection_reason": self.selection_reason,
        }


@dataclass(frozen=True, slots=True)
class ModelRepositoryInspection:
    """Immutable repository provenance and supported download candidates."""

    source_repository: str
    source_revision: str
    license_posture: str
    candidates: tuple[LocalModelChoice, ...]

    def safe_dict(self) -> dict[str, object]:
        """Return inspection data suitable for REST, CLI, notebook, and browser clients."""
        return {
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "license_posture": self.license_posture,
            "candidates": [candidate.safe_dict() for candidate in self.candidates],
        }

    def plan(
        self,
        *,
        cpu_available: bool,
        gpu_available: bool,
    ) -> LocalModelDownloadPlan:
        """Choose the best supported runtime and model file for this deployment."""
        gpu = next(
            (candidate for candidate in self.candidates if candidate.runtime == "vllm"),
            None,
        )
        if gpu_available and gpu is not None:
            return LocalModelDownloadPlan(
                model=gpu,
                selection_reason=(
                    "Selected the repository snapshot because this deployment provides the "
                    "NVIDIA vLLM runtime."
                ),
            )
        cpu = _preferred_gguf(self.candidates) if cpu_available else None
        if cpu is not None:
            return LocalModelDownloadPlan(
                model=cpu,
                selection_reason=(
                    "Selected a balanced single-file GGUF variant for the portable CPU runtime."
                ),
            )
        if gpu is not None:
            raise ModelRepositoryError(
                "This repository requires an NVIDIA vLLM deployment, but the current Heartwood "
                f"runtime is CPU-only. Use a GPU image or report another setup at {_ISSUE_URL}"
            )
        if any(candidate.runtime == "llama-cpp" for candidate in self.candidates):
            raise ModelRepositoryError(
                "This repository provides a CPU model, but the current deployment does not "
                f"include the portable CPU runtime. Report another setup at {_ISSUE_URL}"
            )
        raise ModelRepositoryError(
            "Heartwood cannot prepare this model on the current deployment. "
            f"Report it at {_ISSUE_URL}"
        )


class HuggingFaceModelRepository:
    """Inspect public or process-authenticated Hugging Face model repositories."""

    def __init__(
        self,
        *,
        token: str | None = None,
        model_info: ModelInfoProvider | None = None,
    ) -> None:
        self.token = token
        if model_info is None:
            api = import_module("huggingface_hub").HfApi()
            model_info = cast(ModelInfoProvider, api.model_info)
        self.model_info = model_info

    def inspect(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelRepositoryInspection:
        """Resolve a repository revision and enumerate supported CPU and GPU candidates."""
        repository = repository.strip()
        revision = revision.strip() if revision is not None else None
        if _REPOSITORY.fullmatch(repository) is None:
            raise ModelRepositoryError("repository must use the owner/model format")
        if revision == "":
            revision = None
        try:
            info = self.model_info(
                repository,
                revision=revision,
                files_metadata=True,
                token=self.token,
            )
        except ModelRepositoryError:
            raise
        except Exception as error:
            raise ModelRepositoryError(
                f"unable to inspect Hugging Face model {repository}: {error}"
            ) from error

        resolved_revision = getattr(info, "sha", None)
        if (
            not isinstance(resolved_revision, str)
            or _RESOLVED_REVISION.fullmatch(resolved_revision) is None
        ):
            raise ModelRepositoryError("Hugging Face did not return an immutable model revision")
        source_repository = getattr(info, "id", repository)
        if (
            not isinstance(source_repository, str)
            or _REPOSITORY.fullmatch(source_repository) is None
        ):
            source_repository = repository
        if _requires_custom_code(info):
            raise ModelRepositoryError(
                "Heartwood does not yet support model repositories that require custom code. "
                f"Report the model at {_ISSUE_URL}"
            )
        siblings = getattr(info, "siblings", None)
        if not isinstance(siblings, list) or not siblings:
            raise ModelRepositoryError("the model repository does not expose downloadable files")

        inspected_files: list[_RepositoryFile] = []
        for sibling in siblings:
            file = _repository_file(sibling)
            if file is not None:
                inspected_files.append(file)
        files = tuple(inspected_files)
        license_posture = _license_posture(getattr(info, "card_data", None))
        candidates = [
            candidate
            for item in files
            if (
                candidate := _gguf_candidate(
                    source_repository, resolved_revision, item, license_posture
                )
            )
            is not None
        ]
        snapshot = _snapshot_candidate(
            source_repository,
            resolved_revision,
            files,
            license_posture,
        )
        if snapshot is not None:
            candidates.append(snapshot)
        if not candidates:
            raise ModelRepositoryError(
                "Heartwood does not yet support this model repository. Use a standard "
                "safetensors model for NVIDIA vLLM or a single-file GGUF repository for CPU "
                f"inference, or report the model at {_ISSUE_URL}"
            )
        return ModelRepositoryInspection(
            source_repository=source_repository,
            source_revision=resolved_revision,
            license_posture=license_posture,
            candidates=tuple(candidates),
        )

    def plan(
        self,
        repository: str,
        *,
        cpu_available: bool,
        gpu_available: bool,
        revision: str | None = None,
    ) -> LocalModelDownloadPlan:
        """Inspect and automatically choose one supported local-model configuration."""
        return self.inspect(repository, revision=revision).plan(
            cpu_available=cpu_available,
            gpu_available=gpu_available,
        )


@dataclass(frozen=True, slots=True)
class _RepositoryFile:
    path: str
    size: int
    sha256: str | None


def recommended_model_choices(
    artifacts: tuple[ModelArtifact, ...],
    snapshots: tuple[ModelSnapshot, ...],
    *,
    recommended_only: bool = True,
) -> tuple[LocalModelChoice, ...]:
    """Normalize the centrally configured recommendation catalogs into one ordered list."""
    choices = [
        LocalModelChoice(
            model_id=artifact.artifact_id,
            label=artifact.model_alias,
            purpose=artifact.purpose,
            runtime="llama-cpp",
            source_repository=artifact.source_repository,
            source_revision=artifact.source_revision,
            source_path=artifact.source_path,
            size_bytes=artifact.artifact_size_bytes,
            minimum_free_bytes=artifact.minimum_free_bytes,
            license_posture=artifact.license_posture,
            catalog_source="recommended",
            artifact_sha256=artifact.artifact_sha256,
            minimum_resource_envelope=artifact.minimum_resource_envelope,
            recommended_resource_envelope=artifact.recommended_resource_envelope,
        )
        for artifact in artifacts
        if artifact.recommended or not recommended_only
    ]
    choices.extend(
        LocalModelChoice(
            model_id=snapshot.snapshot_id,
            label=snapshot.model_alias,
            purpose=snapshot.purpose,
            runtime="vllm",
            source_repository=snapshot.source_repository,
            source_revision=snapshot.source_revision,
            source_path=None,
            size_bytes=snapshot.expected_size_bytes,
            minimum_free_bytes=snapshot.minimum_free_bytes,
            license_posture=snapshot.license_posture,
            catalog_source="recommended",
            minimum_resource_envelope=snapshot.minimum_resource_envelope,
            recommended_resource_envelope=snapshot.recommended_resource_envelope,
        )
        for snapshot in snapshots
        if snapshot.recommended or not recommended_only
    )
    for choice in choices:
        choice.validate()
    return tuple(choices)


def _repository_file(value: object) -> _RepositoryFile | None:
    path = getattr(value, "rfilename", None)
    size = getattr(value, "size", None)
    if not isinstance(path, str) or not path or not isinstance(size, int) or size <= 0:
        return None
    lfs = getattr(value, "lfs", None)
    digest = getattr(lfs, "sha256", None)
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        digest = None
    return _RepositoryFile(path=path, size=size, sha256=digest)


def _gguf_candidate(
    repository: str,
    revision: str,
    file: _RepositoryFile,
    license_posture: str,
) -> LocalModelChoice | None:
    if (
        not file.path.casefold().endswith(".gguf")
        or _SPLIT_GGUF.search(file.path) is not None
        or file.sha256 is None
    ):
        return None
    filename = PurePosixPath(file.path).name
    label = filename.removesuffix(".gguf").removesuffix(".GGUF")
    return LocalModelChoice(
        model_id=_model_id(repository, revision, "llama-cpp", file.path),
        label=label,
        purpose=_USER_SELECTED_PURPOSE,
        runtime="llama-cpp",
        source_repository=repository,
        source_revision=revision,
        source_path=file.path,
        size_bytes=file.size,
        minimum_free_bytes=(file.size * 3 + 1) // 2,
        license_posture=license_posture,
        catalog_source="user-selected",
        artifact_sha256=file.sha256,
        minimum_resource_envelope=_cpu_resources(file.size, recommended=False),
        recommended_resource_envelope=_cpu_resources(file.size, recommended=True),
    )


def _snapshot_candidate(
    repository: str,
    revision: str,
    files: tuple[_RepositoryFile, ...],
    license_posture: str,
) -> LocalModelChoice | None:
    paths = {file.path for file in files}
    has_weights = any(
        _SAFETENSORS_WEIGHTS.fullmatch(path) is not None
        or _PYTORCH_WEIGHTS.fullmatch(path) is not None
        for path in paths
    )
    if "config.json" not in paths or not has_weights or len(files) == 0:
        return None
    size = sum(file.size for file in files)
    if size <= 0:
        return None
    label = repository.rsplit("/", maxsplit=1)[-1]
    return LocalModelChoice(
        model_id=_model_id(repository, revision, "vllm", None),
        label=label,
        purpose=_USER_SELECTED_PURPOSE,
        runtime="vllm",
        source_repository=repository,
        source_revision=revision,
        source_path=None,
        size_bytes=size,
        minimum_free_bytes=(size * 3 + 1) // 2,
        license_posture=license_posture,
        catalog_source="user-selected",
        minimum_resource_envelope=_gpu_resources(size, recommended=False),
        recommended_resource_envelope=_gpu_resources(size, recommended=True),
    )


def _model_id(
    repository: str,
    revision: str,
    runtime: LocalModelRuntime,
    source_path: str | None,
) -> str:
    name = repository.rsplit("/", maxsplit=1)[-1].casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:32] or "model"
    identity = "\n".join((repository, revision, runtime, source_path or ""))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"hf-{slug}-{digest}"


def _license_posture(card_data: object) -> str:
    license_id = getattr(card_data, "license", None)
    if not isinstance(license_id, str) and card_data is not None:
        to_dict = getattr(card_data, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if isinstance(data, dict):
                value = data.get("license")
                if isinstance(value, str):
                    license_id = value
    if isinstance(license_id, str) and license_id.strip():
        return f"Source model card reports {license_id.strip()}; review its terms before use."
    return "No machine-readable license was reported; review the source repository before use."


def _requires_custom_code(info: object) -> bool:
    tags = getattr(info, "tags", None)
    if isinstance(tags, list | tuple) and any(
        isinstance(tag, str) and tag.casefold() == "custom_code" for tag in tags
    ):
        return True
    config = getattr(info, "config", None)
    return isinstance(config, dict) and bool(config.get("auto_map"))


def _preferred_gguf(candidates: tuple[LocalModelChoice, ...]) -> LocalModelChoice | None:
    gguf = [candidate for candidate in candidates if candidate.runtime == "llama-cpp"]
    if len(gguf) == 1:
        return gguf[0]
    for marker in _GGUF_PREFERENCE:
        matching = [
            candidate for candidate in gguf if marker in (candidate.source_path or "").casefold()
        ]
        if len(matching) == 1:
            return matching[0]
    if gguf:
        raise ModelRepositoryError(
            "Heartwood found several GGUF files but could not choose a safe default. "
            f"Report this model at {_ISSUE_URL}"
        )
    return None


def _cpu_resources(size: int, *, recommended: bool) -> str:
    model_gib = _round_up_gib(size)
    memory_gib = model_gib * 2 + (8 if recommended else 4)
    disk_gib = model_gib * 2
    cores = 8 if recommended else 4
    prefix = "Recommended" if recommended else "Estimated minimum"
    return f"{prefix}: {cores} CPU cores, {memory_gib} GB RAM, and {disk_gib} GB free storage."


def _gpu_resources(size: int, *, recommended: bool) -> str:
    model_gib = _round_up_gib(size)
    vram_gib = model_gib + (8 if recommended else 4)
    memory_gib = max(32, model_gib * 2 + (16 if recommended else 8))
    disk_gib = model_gib * 2
    prefix = "Recommended" if recommended else "Estimated minimum"
    return (
        f"{prefix}: one NVIDIA GPU with {vram_gib} GB VRAM, {memory_gib} GB RAM, "
        f"and {disk_gib} GB free storage."
    )


def _round_up_gib(size: int) -> int:
    return max(1, (size + 1024**3 - 1) // 1024**3)
